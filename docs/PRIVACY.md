# Privacy: Studio Pulse SDK and collector

What the SDK collects, what it never collects, how consent, opt-out and
deletion work, and what each store and law asks for. This is an engineering
checklist from the research report (12 Nov 2026), **not legal advice**:
everything marked **legal review needed** goes to counsel before a game ships
with the SDK.

## Collected, and where it is declared

The data types, generated from the contract, with their Apple privacy
manifest keys and Google Play Data safety categories, are in
[CONTRACT.md, "Privacy mapping"](CONTRACT.md#privacy-mapping). In short:
product interaction, crash and performance data, other diagnostics (OS,
device model and tier, RAM), a random install id, an optional game account id
(`uid`, never in child mode), purchase history, and the country derived from
the request address.

`contract/generated/PrivacyInfo.xcprivacy` is the privacy manifest of the
SDKs' iOS plug-ins, generated from the same list and copied by the generator into
`unity/com.studio.pulse/Plugins/iOS/` and `flutter/studio_pulse/ios/Resources/`:
no tracking, no tracking domains, every type "linked" (Apple treats data as
linked unless strong protections are in place, and personal data under privacy
laws counts as linked), purposes Analytics and App Functionality, and one
required-reason API: file timestamps with `C617.1`, because the event queue
reads the size of its own files in the app container. The SDKs use no
UserDefaults / PlayerPrefs (Unity declares its own, e.g. `CA92.1` for
PlayerPrefs).
`plutil -lint` passes on it (the test runs it on macOS). A third-party SDK
needs its own manifest when it collects data; since 1 May 2024 App Store
Connect rejects required-reason APIs without declared reasons.

## Never collected

- No advertising or vendor identifiers: no IDFA, AAID, IDFV, Android ID, no
  MAC/IMEI/IMSI/serials, no device fingerprint (Apple forbids fingerprinting
  with or without ATT). The install id is a random UUIDv4 the SDK creates and
  can rotate. The SDK never needs App Tracking Transparency: it does not link
  data with other companies' data or share it with brokers.
- No IP address at rest. The collector uses it in memory to look up the
  country and, as a salted hash, for rate limiting; it is never written to a
  segment, the quarantine, SQLite or BigQuery (a test checks every stored
  byte).
- No e-mail addresses, names or free text from people: string values that look
  like an e-mail are quarantined, `uid` must be a game account id, and child
  mode drops free-text parameters.

## Checklist

| Item | Status in this repository |
|---|---|
| No IDFA/AAID/IDFV/Android ID, no fingerprint, no PII in params | contract and collector: e-mail-like strings quarantined; ids are random |
| IP used in memory for the country only, never stored | collector (tested); CNIL also accepts truncating the last byte |
| Raw events kept at most 13 months, aggregates kept | local: `pulse run` / `pulse collect purge` delete segment days older than 396 days; BigQuery: 396-day partition expiration in `deploy/cloudrun/deploy.sh` |
| Deletion endpoint and process, executed within 90 days at most | `POST /v2/delete`: refused from that moment, gone from the warehouse at the next run, removed from segments by the first run after two hours, request marked done; well inside 90 days if `pulse run` runs daily |
| Consent / opt-out | collector stores nothing from `consent: denied` batches and refuses deleted installs; SDK side below |
| `PrivacyInfo.xcprivacy` in the iOS plug-in, linted | generated from the contract into both SDKs' iOS plug-ins; `plutil -lint` in the tests and on the macOS CI runner |
| Data safety form and App Store privacy labels per game | mapping table in CONTRACT.md; filling the forms per game is manual |
| Privacy policy text: purpose, recipients (Google Cloud), transfer abroad, rights, opt-out | **legal review needed** |
| KVKK transfer-abroad mechanism (standard contract + notification, or region choice) | **legal review needed** |
| Kids games: child mode on, no ad SDKs outside Families-certified ones | `ctx.child` handled by the collector; SDK and ad stack per game |
| Consent default per market (EU/UK opt-in vs CNIL-style opt-out, Turkey) | **legal review needed** |

## What the SDKs expose

Implemented in both SDKs ([SDK_UNITY.md](SDK_UNITY.md#privacy),
[SDK_FLUTTER.md](SDK_FLUTTER.md)) as specified below; tested: consent required
writes nothing to disk or network before consent, opt-out wipes the queue and
keeps only the flag across restarts, a deletion request is retried until the
collector answers and the id rotates, child mode strips `uid` and free text.

| API | Behaviour |
|---|---|
| `Configure(consentRequired, childDirected, regionHint)` | when consent is required, nothing is written to disk or sent until `SetConsent(true)` |
| `SetConsent(bool)` | toggles collection, sends `pulse_consent`, mirrors to Firebase consent when the Firebase sink is on |
| `OptOut()` | stops, wipes the on-device queue, keeps only the opt-out flag |
| `RequestDataDeletion()` | sends `{install_id}` to `/v2/delete`, then rotates the install id |
| `ResetInstallId()` | a new random id |
| `GetInstallId()` | for support requests ("send us this id") |
| child mode | no `uid`, no free text, no ad-attribution fields, analytics only |

The collector already honours the server side of each: `consent: denied`
(nothing stored), `/v2/delete` (tombstone, purge, done marker), `child: true`
(strip `uid` and free text).

## Stores and laws (findings to take to counsel)

- **Google Play Data safety.** SDK collection counts as the app's
  collection; "collection" means transmitting off the device. Relevant
  categories: App activity (app interactions), App info and performance (crash
  logs, diagnostics), Device or other IDs, Financial info (purchase history),
  approximate location (country from IP: whether a country derived from an IP
  counts as approximate location is **legal review needed**). Encryption in
  transit is asked (use HTTPS in production; the local mode is plain HTTP on a
  trusted network), and a deletion mechanism or deletion within 90 days.
  Pseudonymous data must still be disclosed.
- **Families policy.** Child-only apps must not send AAID, SIM/Build serial,
  BSSID, MAC, SSID, IMEI, IMSI, and may use only SDKs approved for
  child-directed services; mixed-audience apps must not send them for
  children or users of unknown age. Whether this SDK needs Families
  self-certification for a child-directed game: **legal review needed**.
- **COPPA (US).** Persistent identifiers are personal information; without
  parental consent they are allowed only for "support for internal
  operations" (which includes analytics to maintain or analyze the service)
  when no other personal information is collected, never for profiles,
  contact or behavioural ads. The rule was amended in 2025 with a one-year
  compliance window. **Legal review needed** for any title aimed at children.
- **GDPR and ePrivacy (EU/UK).** Storing an id on the device falls under
  ePrivacy Art. 5(3) (EDPB Guidelines 2/2023, final 16 Oct 2024). CNIL lets
  audience measurement run without consent (opt-out) only when users are
  informed and can object, the purpose is limited to measurement or A/B tests,
  data is not combined with other processing, it stays with one publisher, the
  IP's last byte is truncated and trackers live at most 13 months. Whether the
  SDK's default may be opt-out per market: **legal review needed**.
- **KVKK (Turkey).** Explicit consent unless an Art. 5 ground applies (for
  example legitimate interest that does not harm fundamental rights); Art. 10
  duty to inform; Art. 11 rights; Art. 7 erase or anonymise when the purpose
  ends. A non-Turkish Google Cloud region is a transfer abroad: since the 2024
  amendment it needs an adequacy decision or safeguards such as a Board
  standard contract notified to the Authority within five business days;
  explicit consent only as a narrow exception. **Legal review needed** for
  the choice of region and mechanism.

## Sources

Apple: privacy manifest files, required-reason APIs, data use in privacy
manifests, App privacy details, User privacy and data use (ATT,
fingerprinting); Unity's Apple privacy manifest policy. Google Play: Data
safety section, Families policy. FTC: COPPA FAQ and the 2025 rule amendments.
EDPB Guidelines 2/2023; CNIL sheet 16 (analytics); KVKK Law No. 6698. Full
URLs are in the research report's source list ([S48]-[S61]).
