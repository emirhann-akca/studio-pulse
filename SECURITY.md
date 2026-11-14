# Security policy

## Reporting a vulnerability

Please report security problems privately, through GitHub's private vulnerability
reporting: open this repository's **Security** tab and choose **Report a vulnerability**
(the `security/advisories/new` page of the repository).

Do not open a public issue, pull request or discussion about a vulnerability before a fix
is released. Include what you found, how to reproduce it (a request, a config file, an
event batch) and what an attacker gains. A proof of concept against your own local
install is enough; please do not test against anyone else's collector.

What to expect: this is a small project maintained by one studio. A report is
acknowledged within 5 working days, triaged within 10, and a confirmed issue is fixed in
a release and published as a GitHub security advisory (with a CVE when it qualifies),
crediting you unless you ask otherwise. If a fix takes longer than 90 days, the advisory
says why.

Good-faith research that follows this policy (no access to other people's data, no
service disruption, no public disclosure before the fix) is welcome and will not be
pursued.

## Supported versions

| Version | Supported |
|---|---|
| `main` and the latest `0.x` release | yes |
| older releases | no: upgrade |

## Scope

In scope: the `studio-pulse` Python package (CLI, `pulse serve` web UI, the desktop app,
`pulse collect` and its Cloud Run deployment, the analyst and its SQL guard, the market
radar), the Unity and Flutter SDKs, the installers (`install.sh`, `install.ps1`) and the
GitHub workflows.

Out of scope:

- The write key being readable from a shipped game: it is a public, write-only key by
  design (docs/COLLECTOR.md). Reports of what it allows beyond writing events are in scope.
- Attacks that need control of the machine running `pulse` (its files, its environment,
  its user account) or of the operator's own configuration files.
- The demo site (docs/demo): synthetic data only.
- Vulnerabilities in third-party services (Google, Apple, ad networks, LLM providers) and
  in dependencies, unless studio-pulse uses them in a vulnerable way; report those upstream.
- Missing hardening without a demonstrated impact (for example a header on a page that
  holds no data).

How the project is defended, and what it does not defend against, is written down in
[docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).
