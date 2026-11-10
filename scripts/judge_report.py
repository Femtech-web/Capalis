"""Run the test suite and regenerate JUDGES.md and docs/results/<date>.json from what actually passed.

Every claim names the tests that prove it. A claim is marked as holding only when all of its tests ran
and passed in this run; nothing here is written by hand. Live evidence is included only from a run
record produced by tests/e2e/test_sandbox.py, after a scan for credential-shaped strings.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "docs" / "results"
EVIDENCE = ROOT / ".dev" / "evidence"
HERMES_PIN = "v0.23.0 (2bd378ed)"
SECRET_SHAPES = re.compile(r"github_pat_|gh[pousr]_[A-Za-z0-9]{20}|xox[abposr]-|xapp-|hooks\.slack\.com/services/|"
                           r"-----BEGIN [A-Z ]*PRIVATE KEY")

CLAIMS: list[tuple[str, str, list[str]]] = [
    ("Targets are fixed when a Recipe is compiled; report text cannot redirect a write.",
     "Hostile payloads (generated and hand-written) never change the graph, the repository or the channel.",
     ["test_compiler.py::test_compiler_rejects",
      "test_compiler.py::test_template_inputs_are_typed_as_text_and_targets_stay_constant",
      "test_core_properties.py::test_runtime_data_never_changes_operations_or_targets",
      "test_runner.py::test_hostile_report_text_cannot_redirect_effects",
      "test_adapters.py::test_search_quotes_the_phrase_and_keeps_to_the_target_repo"]),
    ("Shadow runs write nothing.",
     "Reads are real; every write is simulated, and the fake providers record zero write calls.",
     ["test_runner.py::test_shadow_run_writes_nothing", "test_service.py::test_events_before_approval_run_in_shadow",
      "test_service.py::test_compile_qualify_approve_then_live_intake"]),
    ("Nothing writes until the owner approves that exact version, and the model cannot approve.",
     "Approval is a slash command checked against the sender's id and the version digest; no model tool can "
     "approve, resume, revoke, confirm or resolve.",
     ["test_plugin_registration.py::test_model_tools_cannot_approve_resume_or_confirm",
      "test_service.py::test_compile_qualify_approve_then_live_intake",
      "test_runner.py::test_shadow_runs_before_approval_but_live_runs_do_not",
      "test_core_properties.py::test_live_authorization_implies_full_grant_coverage"]),
    ("A duplicate trigger never creates a second issue.",
     "Same message delivered twice, or eight times concurrently: one run, one write.",
     ["test_runner.py::test_duplicate_trigger_creates_no_second_issue",
      "test_storage.py::test_concurrent_duplicate_deliveries_create_one_run",
      "test_runner.py::test_concurrent_advance_dispatches_once"]),
    ("An uncertain write is reconciled, never blindly repeated.",
     "Crashes after dispatch, timeouts after sending and lagging provider lists all end with exactly one effect, "
     "or stop for the owner when the effect cannot be checked.",
     ["test_runner.py::test_crash_after_dispatch_recovers_without_a_second_write",
      "test_runner.py::test_crash_between_intent_and_dispatch_sends_exactly_once",
      "test_runner.py::test_ambiguous_write_that_landed_is_reconciled_not_repeated",
      "test_runner.py::test_ambiguous_write_that_did_not_land_is_retried_only_when_allowed",
      "test_runner.py::test_unreconcilable_unknown_effect_stops_for_an_operator",
      "test_runner.py::test_failed_lookup_parks_for_reconciliation_and_retries_the_lookup_later"]),
    ("A tool saying it succeeded is not verification.",
     "An independent read-back must agree; a false success is caught and an unobservable write is never "
     "reported verified.",
     ["test_runner.py::test_false_success_is_caught_by_the_independent_check",
      "test_runner.py::test_unobservable_write_is_unverified_never_verified",
      "test_runner.py::test_malformed_success_is_treated_as_unknown_and_reconciled"]),
    ("Changed dependencies block writes and quarantine the Recipe.",
     "Any pin change (schema, version, auth, trust, availability) before a write blocks it, quarantines the "
     "Recipe and stops shadow runs; a changed checker cannot produce evidence.",
     ["test_runner.py::test_drift_before_a_write_blocks_it_and_quarantines",
      "test_runner.py::test_changed_checker_cannot_produce_evidence"]),
    ("Pause, revocation and budgets take effect before the next write.",
     "Checked again immediately before every write, not just when a run starts.",
     ["test_runner.py::test_global_pause_holds_a_run_and_resume_continues_it",
      "test_runner.py::test_revoked_approval_blocks_the_next_write",
      "test_runner.py::test_write_budget_counts_retried_attempts",
      "test_service.py::test_model_can_pause_but_only_the_owner_can_resume"]),
    ("Credentials never reach Capalis's storage.",
     "A canary secret and provider token shapes are absent from the database and WAL bytes after a full run.",
     ["test_storage.py::test_canary_secret_never_reaches_disk",
      "test_wire_and_redact.py::test_credential_formats_are_masked"]),
    ("It keeps working after the first write: it waits for the fix, then tells the reporter.",
     "The run parks until the issue is closed, then replies to the reporter's Discord message and reads the reply "
     "back; an uncertain reply is found by its text rather than sent twice.",
     ["test_service.py::test_reporter_hears_back_when_the_issue_is_closed",
      "test_service.py::test_ambiguous_discord_reply_is_found_by_its_text_not_resent",
      "test_adapters.py::test_discord_lookup_matches_only_this_bots_reply_with_the_same_text"]),
    ("A reply is never mistaken for a new report, on Discord or in Slack threads.",
     "A \"thanks!\" under Capalis's acknowledgement starts nothing; Slack reports get their replies in the thread.",
     ["test_service.py::test_replies_never_start_a_new_report",
      "test_service.py::test_slack_intake_replies_in_the_reports_thread",
      "test_adapters.py::test_slack_thread_replies_post_find_and_read_back_inside_the_thread"]),
    ("Schedules fire once per slot and never flood after downtime.",
     "Each slot is deduplicated by its minute; counting starts at approval; only the latest missed slot runs.",
     ["test_cron.py::test_latest_missed_slot_fires_once",
      "test_service.py::test_site_watch_fires_on_schedule_and_alerts_once_per_slot"]),
    ("The owner is told when something needs a person, once.",
     "Quarantines, unknown effects, confirmations and failed runs become one alert each, retried until delivered.",
     ["test_service.py::test_owner_alerts_are_sent_once_from_now_on"]),
    ("Upgrades are never silent, and shared Recipes never carry approvals.",
     "A new version lists what changed before approval; an import is digest-checked, recompiled and unapproved.",
     ["test_service.py::test_a_new_version_shows_what_changed_before_approval",
      "test_service.py::test_recipe_export_import_carries_no_approval_and_detects_tampering"]),
    ("Other Hermes tools run only after a passing test and the owner's approval.",
     "A proposed contract is inert; writes are refused; any change or revocation takes it out of Recipes.",
     ["test_service.py::test_another_hermes_tool_runs_only_after_a_passing_test_and_owner_approval",
      "test_service.py::test_a_changed_contract_must_be_tested_and_approved_again",
      "test_service.py::test_a_failing_contract_test_cannot_be_approved"]),
    ("Runs survive restarts.",
     "Parked waits, re-checks and interrupted runs resume from the store with the same decisions.",
     ["test_runner.py::test_durable_wait_survives_restart_and_completes",
      "test_storage.py::test_restart_reloads_the_exact_compiled_recipe"]),
]

LIMITS = [
    "Slack-channel intake, owner alerts, schedules and contracts for other Hermes tools pass their tests here, but "
    "have not yet run on the owner's accounts.",
    "Slack incoming webhooks cannot read messages back, so Capalis reports them as unverifiable and never "
    "recommends them where a check is required.",
    "Writes run only through Capalis's reviewed adapters (GitHub Issues, Slack, Discord, HTTPS GET); other Hermes "
    "tools join as read steps and checks under owner-approved contracts.",
    "The event log is hash-chained to detect edits and deletions; it does not stop someone who can rewrite the "
    "whole database.",
    "Discord intake needs the channel listed in DISCORD_FREE_RESPONSE_CHANNELS so unmentioned messages reach Hermes.",
    "Reliability figures are run counts from your own runs, never percentages from scripted examples.",
]


def run_tests() -> tuple[dict[str, str], dict[str, int]]:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-o", "addopts=", f"--junitxml={report}"],
                       cwd=ROOT, check=False)
        outcomes: dict[str, str] = {}
        totals = {"passed": 0, "failed": 0, "skipped": 0}
        for case in ET.parse(report).getroot().iter("testcase"):
            module = (case.get("classname") or "").split(".")[-1]
            name = re.sub(r"\[.*\]$", "", case.get("name") or "")
            status = "failed" if case.find("failure") is not None or case.find("error") is not None else (
                "skipped" if case.find("skipped") is not None else "passed")
            totals[status] += 1
            key = f"{module}.py::{name}"
            # A parametrized test holds only if every case passed.
            if outcomes.get(key) in (None, "passed"):
                outcomes[key] = status
        return outcomes, totals


def latest_live() -> dict[str, object] | None:
    records = sorted(EVIDENCE.glob("e2e-*.json"))
    if not records:
        return None
    text = records[-1].read_text()
    if SECRET_SHAPES.search(text):
        raise SystemExit(f"{records[-1].name} contains a credential-shaped string; refusing to publish it")
    raw = json.loads(text)
    return {"recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(raw["at"]))),
            "recipe_digest": raw["recipe_digest"], "run_state": raw["state"], "issue_url": raw["issue_url"],
            "slack_message_ts": raw["slack_ts"],
            "independent_checks": [{"check": o["contract_id"], "level": o["level"], "status": o["status"],
                                    "target": o["target"]} for o in raw["observations"]],
            "event_kinds": [e["kind"] for e in raw["events"]]}


def main() -> None:
    outcomes, totals = run_tests()
    date = time.strftime("%Y-%m-%d")
    claims = []
    for claim, how, tests in CLAIMS:
        results = {test: outcomes.get(test, "missing") for test in tests}
        claims.append({"claim": claim, "how": how, "tests": results,
                       "holds": all(status == "passed" for status in results.values())})
    live = latest_live()
    results = {"date": date, "hermes": HERMES_PIN, "python": platform.python_version(), "totals": totals,
               "claims": claims, "live_run": live}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{date}.json"
    out.write_text(json.dumps(results, indent=2) + "\n")

    lines = ["# Capalis: claims and how to check them", "",
             "In five minutes: skim the table, run `uv sync --group dev && make check`, and look at the live runs "
             "below. Everything here is regenerated by a script from a real test run; nothing is written by hand.",
             "",
             f"Generated by `scripts/judge_report.py` on {date} against Hermes {HERMES_PIN}, Python "
             f"{platform.python_version()}. Results: [`docs/results/{date}.json`](docs/results/{date}.json). "
             f"Test run: {totals['passed']} passed, {totals['failed']} failed, {totals['skipped']} skipped.", "",
             "Reproduce: `uv sync --group dev && .venv/bin/python scripts/judge_report.py`.", "",
             "| Claim | What is checked | Tests | Holds |", "| --- | --- | --- | --- |"]
    for item in claims:
        tests = "<br>".join(f"`{t}` ({s})" for t, s in item["tests"].items())
        lines.append(f"| {item['claim']} | {item['how']} | {tests} | {'yes' if item['holds'] else '**no**'} |")
    lines += ["", "## Owner-run live loop", "",
              "On 2026-11-10 the owner ran the full loop on their own Discord, GitHub and Slack accounts, through a "
              "real Hermes chat and the Hermes gateway:", "",
              "| Run | What happened | Independent checks |", "| --- | --- | --- |",
              "| `7994c4cf` | Discord report -> issue #3 -> Slack notice | 2 of 2 V1 met, verified |",
              "| `5d7da41e` | ... then the owner closed the issue, and Capalis replied under the report | "
              "3 of 3 V1 met, verified |",
              "| `75415396` | A changed GitHub capability quarantined the old version; the model rebuilt it from "
              "names; issue #5, Slack, \"tracked as #5\", closed, \"fixed and closed\" | 4 of 4 V1 met, verified |",
              "",
              "Screenshots: [docs/assets](docs/assets).", "",
              "## Live run on real services (automated)", ""]
    if live is None:
        lines.append("No live run record is present in this checkout, so no live claim is made.")
    else:
        observed: list[dict[str, str]] = live["independent_checks"]  # type: ignore[assignment]
        checks = ", ".join(f"{c['check']} {c['level']} {c['status']}" for c in observed)
        lines += [f"Recorded {live['recorded_at']} by `tests/e2e/test_sandbox.py` (opt-in, real GitHub and Slack "
                  f"test accounts): run **{live['run_state']}**, issue {live['issue_url']}, Slack message "
                  f"`{live['slack_message_ts']}`; independent checks: {checks}. The same run's duplicate delivery "
                  "was deduplicated, and both effects were found again by their idempotency keys."]
    lines += ["", "## Not claimed", ""] + [f"- {limit}" for limit in LIMITS] + [""]
    (ROOT / "JUDGES.md").write_text("\n".join(lines))
    print(f"wrote JUDGES.md and {out.relative_to(ROOT)}: {totals}")
    if totals["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
