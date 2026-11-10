# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Security → Report a vulnerability** on this repository; don't open a public issue. Include the Capalis and Hermes versions, what you did, and what happened. We aim to acknowledge reports within 72 hours.

Particularly relevant:
- any way for untrusted text to change where a Recipe writes,
- any way for the model to approve, resume or confirm,
- any way to cause a duplicate external write,
- any credential that reaches the Capalis database, logs or evidence.

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |

## How Capalis is built to be safe

See [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md): who is trusted with what, the twelve invariants with where each is enforced and the test that proves it, the threats considered, and what is out of scope.
