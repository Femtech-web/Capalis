.PHONY: install lint typecheck test check judge e2e validate

install:
	uv sync --group dev

lint:
	.venv/bin/ruff check .

typecheck:
	.venv/bin/mypy

test:
	.venv/bin/pytest

check: lint typecheck test

judge:
	.venv/bin/python scripts/judge_report.py

# Live run on your own sandbox accounts; needs CAPALIS_GITHUB_TOKEN and CAPALIS_SLACK_BOT_TOKEN.
e2e:
	CAPALIS_E2E=1 .venv/bin/pytest tests/e2e

# Requires a local Hermes v0.23.0 on PATH.
validate:
	hermes plugins validate plugin --install-deps
	hermes plugins doctor plugin --ci
