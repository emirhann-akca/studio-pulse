# Unity SDK (`unity/com.studio.pulse`)

The studio's own analytics SDK for Unity games, on [event contract v2](CONTRACT.md),
sending to [`pulse collect`](COLLECTOR.md). One UPM package with two assemblies:

- `Studio.Pulse.Core` (`Runtime/Core`, `noEngineReferences`): pure C# for
  netstandard 2.1. Queue, batching, retries, sessions, ids, privacy, errors, JSON.
  Tested without Unity (Mono here, `dotnet test` in CI on Windows, macOS and Linux).
- `Studio.Pulse` (`Runtime`): the Unity adapter. `Pulse.Init`, UnityWebRequest
  transport, lifecycle and log hooks, Android JNI, the iOS Swift plug-in, and the
  existing perf telemetry (`PerfSampler`, `LoadTimer`), whose `pulse_perf` and
  `pulse_load` events now go through the same queue.

## Quick start (3 lines)

Package Manager → *Add package from git URL*:
`https://github.com/emirhann-akca/studio-pulse.git?path=/unity/com.studio.pulse`
(Unity 2021.3+; every file ships with a `.meta`, so the immutable git install
imports everything). The *Quick Start* sample in the Package Manager shows this:

```csharp
[RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
static void Boot() => Pulse.Init("sp_pub_...", "https://collector.example.com"); // 1. start
Pulse.LevelStart(12);                                                                // 2. report
Pulse.LevelFail(12, "hit_spike", new Vector2(0.42f, 0.77f), progress: 0.8f);         // 3. report
```

On a laptop: `pulse collect --host 0.0.0.0 --key <key>` and point the game at
`http://<laptop-ip>:8787` (see COLLECTOR.md).

## API

All calls are static on `Pulse`, safe before `Init` (no-ops), never throw, and may be
called from any thread (network requests start on the main thread).

| Call | Sends |
|---|---|
| `Init(writeKey, endpoint, PulseOptions options = null, PulseConfig perf = null)` | starts everything below |
| `LevelStart(level, levelName, world, mode)` | `level_start`; the SDK counts `attempt` per level (persisted, reset after a win) |
| `LevelComplete(level, score, stars, movesLeft)` | `level_complete` with `attempt` and `duration_ms` (foreground time since the start) |
| `LevelFail(level, reason, [Vector2 position], progress, worldSpace, score)` | `level_fail` with `pos_x`, `pos_y`, `pos_space` (`norm` 0-1, or `world`): the death heatmap |
| `LevelQuit(level, reason, [position], progress)` | `level_quit` (`menu`, `restart`); a level open when a session times out is closed with `background_timeout` by the SDK |
| `TutorialBegin(tutorial)`, `TutorialStep(step, stepName, tutorial)`, `TutorialComplete(tutorial)` | `tutorial_*` |
| `SettingChange(setting, from, to)` / `SettingChange(setting, bool from, bool to)` | `setting_change`; `setting` is `music`, `sfx`, `haptics`, `notifications`, `language` or `quality` (`PulseContract.Setting`) |
| `EarnCurrency(currency, amount, item, level)`, `SpendCurrency(...)` | `earn_virtual_currency`, `spend_virtual_currency` (GA4 names) |
| `Purchase(transactionId, productId, priceMicros \| decimal price, currency, isRestore, isSandbox)` | `purchase` (Unity IAP: `metadata.localizedPrice`, `isoCurrencyCode`) |
| `AdImpression(adPlatform, adFormat, value, currency, adSource, adUnitName, placement, precision)` | `ad_impression` (MAX `adInfo.Revenue`, LevelPlay `impressionData.revenue`) |
| `AdEvent(action, adFormat, placement, errorCode)` | `ad_event` (`request`, `fill`, `show`, `click`, `reward`, `fail`) |
| `Screen(name, screenClass)` | `screen_view` |
| `Track(name, PulseParams)` | a custom event: name `[a-z][a-z0-9_]{0,39}`, not `pulse_*`, not a Firebase-reserved name or prefix, at most 25 int / double / bool / string params. Reuse one `PulseParams` (`Clear()`) and it allocates nothing |
| `ReportException(ex, fatal)` | `pulse_error` for a handled exception |
| `SetUserId(gameAccountId)` | `uid` on later events (refused if it looks like an e-mail; never in child mode) |
| `SetConsent(bool)`, `OptOut()`, `RequestDataDeletion()`, `ResetInstallId()`, `GetInstallId()` | privacy, below |
| `Flush()`, `Stats` | send now (subject to backoff); local counters (queued, sent, dropped, rejected, last status) |

Invalid input (unknown setting or ad action, NaN values, bad names) is counted in
`Stats.Rejected` and logged once, never sent to be quarantined.

## Captured automatically

| Event | When |
|---|---|
| `pulse_install` | first launch of an install id, with `install_store`, `installer_raw`, `restored` |
| `pulse_session_start` | every cold start, and a return after more than 30 min in the background (`cold`, `since_last_s`); a session never spans more than 24 h |
| `pulse_background` / `pulse_foreground` | `OnApplicationPause`; `fg_ms` counts foreground time only |
| `pulse_heartbeat` | every 60 s in the foreground, only when nothing else was recorded in that minute |
| `pulse_app_update` | the version differs from the previous launch (`from_version`) |
| `pulse_error` | `Application.logMessageReceivedThreaded` with `LogType.Exception`: type, scrubbed message (paths, e-mails, ids, numbers removed), top frame, a 64-bit FNV-1a fingerprint; at most 10 unique per launch, each once per session |
| `pulse_exit` | next launch: Android 11+ `ApplicationExitInfo` (`crash`, `crash_native`, `anr`, `low_memory`); elsewhere `unclean` when the previous process died in the foreground without a quit; `crash` after a fatal error |
| `pulse_perf`, `pulse_load` | the perf sampler (30 s frame-time windows, memory) and cold start / scene loads (`Pulse.MarkInteractive()`, `LoadTimer.LoadSceneAsync`) |
| `pulse_consent` | consent granted |

Context on every batch: app id, platform, version and build, build env
(`release`, `debug`, `editor`), install store, Unity version, OS, device model,
device tier (`low` / `mid` / `high` from RAM, cores, GPU memory), RAM, locale, time
zone offset, consent state, child flag.

**Install store.** Android: `PackageManager.getInstallSourceInfo(pkg).getInstallingPackageName()`
on API 30+ (`getInstallerPackageName` before) through `AndroidJavaObject`, no Java plug-in;
`com.android.vending` → `google_play`, Amazon, Samsung, Huawei and Xiaomi stores mapped,
none → `sideload`, anything else → `unknown` with `installer_raw`. iOS
(`Plugins/iOS/StudioPulseInstallSource.swift`): `AppTransaction.shared.environment` on
iOS 16+ (`production` → `app_store`, `sandbox` → `testflight`, `xcode` → `sideload`),
asked once in the background and applied when it answers (StoreKit may need the network;
`AppTransaction.shared` never prompts for a sign-in, only `refresh()` would, and it is
not called); until then, and before iOS 16, the receipt file name decides
(`sandboxReceipt` → `testflight`); the simulator reports `simulator`. The Editor reports
`editor`.

## Delivery

- **Queue on disk** (`Application.persistentDataPath/studio-pulse/queue`): append-only
  segment files, one context line then one event per line, each event written with one
  `write` and flushed to the OS, so a killed process loses nothing it recorded; fsync
  when a segment closes and on every pause. A torn last line (power loss) is cut on the
  next start. A segment is deleted only after the collector answered 2xx for every event
  in it. Cap: 10,000 events / 5 MB; above it the oldest segments go first and are
  counted (`Stats.Dropped`, persisted).
- **Batches**: at 20 events, every 30 s, on pause and on `Flush()`; at most 100 events and
  900 KB per request, gzip from 1 KB, one request at a time.
- **Answers**, the same in release and debug builds:

  | Status | SDK |
  |---|---|
  | 2xx | the events leave the queue (`quarantined` in the answer is counted as rejected) |
  | 413 | halve the batch size for this launch and resend at once; a single event that still does not fit is dropped and counted |
  | 400 and other permanent 4xx | drop the batch, count it, keep the reason (`Stats.LastError`) |
  | 401 / 403 | stop sending until the next launch; the queue stays on disk |
  | network error, 404, 408, 429, 5xx | keep the batch; retry after 2 s, 4 s, 8 s ... 60 s plus 1-5 s random jitter, or `Retry-After` if longer |
- **Ids**: `install_id` UUIDv4, event and session ids UUIDv7 (time-ordered), `seq` per
  install. `seq` is persisted before any segment is deleted and recovered from the queue
  at start, so it is monotonic across restarts and kills and a gap really means loss; the
  collector dedupes on the event id and on `(install_id, seq)`.
- **Clock**: every event carries the device time, every batch `sent_at`; the collector
  corrects skew (`received_at - (sent_at - ts)`).
- **Restores**: Android Auto Backup restores `persistentDataPath` but never
  `getNoBackupFilesDir()`; on iOS the marker next to the state is excluded from backups
  (`Device.SetNoBackupFlag`). Install id present but marker missing → a restored copy:
  `pulse_install` with `restored = true` under a **new** id (two phones must never share
  an id and a `seq` space). The restored queue is deleted (the original device sends it).
- **One process per data directory**: the queue assumes it is the only writer. On
  Windows a queue file another process is writing cannot even be read, so two copies of
  a desktop game running at once from the same `persistentDataPath` are not supported
  (CI runs the kill tests on Windows by closing the dead process's file first, as the OS
  does).

## Privacy

What the SDK collects and the Apple / Google Play mapping: [PRIVACY.md](PRIVACY.md) and
[CONTRACT.md](CONTRACT.md#privacy-mapping). No IDFA, IDFV, AAID or Android ID, no
fingerprinting, no ATT prompt needed.

| | Behaviour |
|---|---|
| `PulseOptions.ConsentRequired = true` | nothing is written to disk or sent before `SetConsent(true)`; up to 500 events wait in memory and are kept (in order) on consent, dropped on refusal |
| `SetConsent(false)` / `OptOut()` | deletes the queue and the state; only `{"opted_out": true}` stays, so the next launch stays silent |
| `SetConsent(true)` after an opt-out | a new install id (and `pulse_install`) |
| `RequestDataDeletion()` | `POST /v2/delete {install_id}` (retried until the collector answers), deletes the local queue, continues under a new id |
| `ResetInstallId()` | a new id; already queued events keep the old one |
| `PulseOptions.ChildDirected = true` | `ctx.child = true`, no `uid`, no free-text params (`level_name`, `step_name`, error messages, custom string params) |

**Privacy manifest and the iOS native part (opt-in)**: `Plugins/iOS/PrivacyInfo.xcprivacy`
(generated from `contract/pulse_v2.yaml`): the collected data types, linked, not tracking,
purposes Analytics and App Functionality, and the file timestamp API with reason `C617.1`
(the queue reads its own file sizes). It and the Swift install-source plug-in are included
only when the project defines **`STUDIO_PULSE_IOS_NATIVE`** (Player Settings > Scripting
Define Symbols, iOS): Unity merges plug-in manifests into the app's, so an always-included
manifest would make an app that never starts the SDK declare collected data. Without the
define the SDK still runs on iOS; the install store is `unknown` and AppTransaction is not
asked. Turn it on together with the App Store privacy label. The SDK uses no
PlayerPrefs / UserDefaults. **Google Play Data safety**: declare App interactions, Crash
logs, Diagnostics, Device or other IDs, Purchase history (if you report purchases),
Approximate location (country from the IP, which is never stored).

## Performance

Measured on an M4 under Unity's Mono 6.13: recording an event (serialize, append, one
`write`) about 4 µs; `Tick()` when idle about 21 ns; building the first gzipped
100-event batch about 11 ms including JIT and zlib loading (later batches are cheaper).
Recording allocates nothing once buffers have grown: a test records 50 events of five
kinds with `GC.GetAllocatedBytesForCurrentThread()` and expects 0 bytes (Mono here,
.NET 10 in CI). Doubles are written in the shortest round-trip form with exact integer
arithmetic, not the runtime's formatter (Mono's is not always correctly rounded).

## Tests

```bash
unity/dotnet/mono-test.sh          # core + tests + driver with the Editor's Mono (no dotnet needed)
unity/dotnet/unity-compile.sh      # the adapter against UnityEngine*.dll for standalone, Android, iOS, WebGL, -warnaserror
dotnet test unity/dotnet/Studio.Pulse.Core.Tests   # what CI runs on Windows, macOS, Linux
```

35 cases: kill mid-flush (no loss, the re-sent batch deduped), a torn line, 24 h
offline replay, 413 halving, 400 drop, 401 disable, 429/5xx backoff timings and
Retry-After with a fake clock, flush triggers, gzip threshold, queue cap, 30-minute and
24-hour session rules, heartbeat, `seq` across six restarts, install / update / exit
reasons, backup restore, attempts and durations, opt-out, consent, deletion, child mode,
errors, validation, zero allocation, 3,093 doubles against Python, string escaping, the
cross-SDK golden (below) and an end-to-end run against a real `pulse collect`
(`PULSE_E2E_URL`). `Studio.Pulse.Driver --soak` records until it is killed: three
`SIGKILL`s during sending, then a normal run, delivered 2,762 events with `seq` 1..2,762,
no gap and no repeat (this Mac, 12 Nov 2026).

**Cross-SDK conformance**: `contract/golden/sdk_script.json` replayed with a fake clock
and SplitMix64 must give exactly `contract/golden/sdk_conformance.ndjson`, in C# and in
Dart; `tests/test_sdk_conformance.py` checks that file against the JSON Schema and the
collector, which stores its 31 events once.

## Not verified on a device

Compiled and unit-tested only; a smoke test on real hardware is still needed for:
the Android JNI calls (installer of record, `getHistoricalProcessExitReasons`,
`getNoBackupFilesDir`, version code) on Android 9, 11 and 14; the Swift plug-in in a
real Unity iOS build (Swift sources from a package, `@_cdecl` symbols, the
TestFlight `sandbox` answer and the App Store `production` answer); pause / resume and
the 30-minute rule on real devices; WebGL (the queue then lives in the browser's
IndexedDB-backed file system, untested). The Unity Editor itself has not opened the
package: compile checks use the Editor's UnityEngine assemblies.
