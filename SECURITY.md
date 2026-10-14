# Security policy

## Supported versions

`agent-personal-memory` is pre-1.0 (version `0.x`). Only the most recent
released version receives security fixes; there is no backport or
long-term-support line yet. The current version is tracked in
`pyproject.toml` and `CHANGELOG.md`.

## Reporting a vulnerability

Please do **not** open a public issue for a suspected vulnerability.
Report it privately through GitHub: open the repository's **Security** tab
on `samohod4ik/agent-personal-memory` and use **"Report a vulnerability"**
(private security advisory). If private advisories are unavailable, reach
the maintainer via the contact options on
<https://github.com/samohod4ik>.

A useful report includes the affected version or commit, a reproduction
path or proof of concept, and the impact you believe it has. Reports are
acknowledged as time permits and disclosure is coordinated with the
reporter.

## Scope

The security model — assets, trust boundaries, confirm-gate mechanics, and
honest limits (at-rest-only encryption, plaintext KEK custody, FTS index
contents, outbox retention) — is documented in
[docs/threat-model.md](docs/threat-model.md). Reports within the
documented honest limits are still welcome context, but they are known
constraints rather than vulnerabilities.
