# Security model

What studio-pulse protects, from whom, where the trust boundaries are, how each one is
defended (with the code that does it) and what is knowingly not defended. Reporting a
problem: [SECURITY.md](../SECURITY.md).

## Assets

| Asset | Where it lives |
|---|---|
| Business data: revenue, installs, retention, UA spend, reviews | the DuckDB warehouse, the static dashboard (`site_dir`), the UI |
| Player-level SDK events (install ids, sessions, purchases) | the collector's NDJSON segments or BigQuery, `stg_pulse_events` |
| Credentials: Google ADC / service account, App Store Connect key path, Slack webhook, SMTP password, LLM API keys, ad-network tokens | the environment, `pulse.settings.env` (created 0600), the operator's key files; never `pulse.yaml` |
| The UI session: the per-process CSRF token, the access token of a network bind | memory of the `pulse serve` process, pages it serves |
| Integrity of decisions: alerts, insights, analyst answers | the warehouse and the LLM ledger |
| Money: LLM budget, cloud query cost | provider accounts (budgets enforced by the LLM client) |
| Players' privacy | the SDKs (consent, opt-out, deletion, no IP stored) |

## Trust boundaries and attackers

| # | Boundary | Attacker |
|---|---|---|
| B1 | Browser → local UI (`pulse serve`, the desktop app) | **A1** a web page the operator visits (CSRF, DNS rebinding, embedding) |
| B2 | Network → a UI bound to a non-loopback address | **A2** another machine on the LAN |
| B3 | Internet → collector (`pulse collect`, Cloud Run) | **A3** anyone with a shipped game (the write key is public) |
| B4 | Third-party text → HTML, SQL, LLM prompts | **A4** whoever writes a store listing, a review, an app or campaign name, an SDK event |
| B5 | studio-pulse → outbound HTTP (stores, Google, ad networks, LLMs, Slack) | **A4** through store data; a mistaken or shared config |
| B6 | Dependencies, GitHub Actions, vendored htmx → the package and CI | **A5** a compromised release or action; **A6** a malicious pull request |

Trusted: the operator, the machine and account that run `pulse`, `pulse.yaml` and the
environment (a config can point the package at any file or service; that is its job).

## Mitigations

**B1/B2, the local UI** (`serve/security.py`, one middleware in front of every route,
mount and error page):

- binds 127.0.0.1 by default; a Host header that is not this machine is refused (DNS
  rebinding);
- bound to any other address, every request needs a random access token (printed once in
  the URL, then an HttpOnly SameSite=Strict cookie); `/healthz` alone answers without it;
- CSRF: every state-changing request needs the per-process token in a header (pages send
  it through htmx; another site cannot read it), an `Origin` equal to the server's own and
  no cross-site Fetch Metadata; routes check the token too. A test enumerates every route
  with an unsafe method (`tests/test_ui_security.py`);
- Fetch Metadata: cross-site subresource requests (an `<img src=/ask?q=...>` that would
  spend the LLM budget) are refused, and `GET /ask` answers only same-origin or typed URLs;
- headers on every response: a Content-Security-Policy whose scripts are `'self'` plus the
  SHA-256 of the two inline scripts the package writes (no `'unsafe-inline'`, no eval;
  htmx runs with `allowEval` off), `frame-ancestors 'none'`/`X-Frame-Options: DENY`,
  `nosniff`, `Referrer-Policy: no-referrer`, COOP/CORP same-origin, `no-store` on HTML;
- Jinja autoescaping everywhere; every `|safe` renders either a package constant (icons,
  CSS, JS) or HTML built by `dashboard/charts.py`, which escapes every label and value;
- the desktop app's `/__app/*` endpoints refuse other hosts and cross-site requests, and
  `app.json` may only name `http://127.0.0.1:<port>/`.

**B3, the collector** (`collector/`): write-only keys, request size limits and gzip
bombs refused, per-IP and per-install rate limits, schema validation with a quarantine,
no IP address stored, `/live` behind an optional read key, a warning when it listens on
the network with the default key. Deletion requests are honoured end to end. SDK events
are client-made: SDK revenue is a signal, store and RevenueCat revenue are the record.

**B4, untrusted text**:

- HTML: autoescaping (above) and the CSP as a second layer;
- SQL: the analyst's model-written SQL goes through an AST allow-list (`brain/guard.py`:
  one SELECT, allow-listed tables and functions, no table functions, file paths, other
  schemas, PRAGMA/SET/COPY/ATTACH), then a read-only DuckDB session with external access
  off and the configuration locked, row and time limits (`brain/executor.py`); the
  injection set (`brain/eval/inject.yaml`) plants instructions in reviews and campaign
  names and runs a model that obeys them;
- LLM output: the radar classifier keeps a claim only if its value is in the taxonomy and
  its quote is really in the store text, with no numbers, links or markup and only
  screenshots that were sent (`radar/classify.py`); narrations with unsupported numbers,
  links or markup are dropped (`radar/digest.py`); nothing a model answers becomes SQL
  outside the guard, a file path, a tool call or a request.

**B5, outbound requests**: the radar fetches only https from the store hosts and checks
every redirect (`radar/http.py`), honours robots.txt and budgets; ids from store data
are URL-encoded; URLs from configuration are checked before a secret is sent
(`netguard.py`: no metadata or link-local addresses, no credentials in URLs, an LLM API
key never in clear text off the local network, the Slack webhook https only and not
redirected); SMTP STARTTLS verifies the server certificate; secrets travel in headers
and are redacted from errors, logs, ledgers and caches (`llm/base.py`).

**B6, supply chain** (`.github/`): every action pinned to a commit SHA and every tool
binary to a SHA-256; least-privilege `permissions:` per job, `persist-credentials: false`;
CodeQL (Python, Actions, C#), osv-scanner on the newest and the lowest dependency versions
`pyproject.toml` allows (its floors are the first releases without a known
vulnerability), gitleaks over the whole history, bandit, dependency review on pull
requests, OpenSSF Scorecard, Dependabot with a 7-day cooldown; htmx is vendored with its
license and served from the package.

## Known gaps (accepted for now)

- One studio, one operator: no user accounts or roles; anyone who has the UI (or its
  access token) has all of it.
- `style-src 'unsafe-inline'` remains: charts and templates use style attributes.
- `netguard` does not resolve host names: a public name that resolves to a private address
  is not caught (the radar pins its hosts; other URLs come from the operator).
- The write key is public by design, so events and SDK revenue can be forged within what
  a key is allowed to write; the collector bounds the rate, not the truth.
- An analyst query can still be expensive within its limits (watchdog timeout, row cap).
- CodeQL does not analyse Dart; the Flutter SDK is covered by its tests and review only.
- The radar reads public Google Play pages; their terms of service are the operator's call
  (docs/RADAR.md).
