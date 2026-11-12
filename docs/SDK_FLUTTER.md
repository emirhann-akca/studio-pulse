# Flutter SDK (`flutter/studio_pulse`)

The studio's analytics SDK for Flutter apps, on [event contract v2](CONTRACT.md), the
same behaviour as the [Unity SDK](SDK_UNITY.md) rule for rule (a shared golden file
proves both write the same bytes). The package was `studio_pulse_perf`; its perf API
is unchanged (`package:studio_pulse/studio_pulse_perf.dart`).

- `package:studio_pulse/core.dart`: the pure-Dart core (no Flutter import), also usable
  from a Dart CLI or server: queue, batching, retries, sessions, ids, privacy, errors.
- `package:studio_pulse/studio_pulse.dart`: the `StudioPulse` facade with
  `AppLifecycleListener`, `FlutterError.onError` and `PlatformDispatcher.instance.onError`
  hooks, a once-a-second tick, `PulsePerf` (frame times, cold start) feeding the same
  queue, and a small platform channel (Kotlin, Swift) for what Dart cannot read.

No third-party dependency: not `path_provider`, `shared_preferences` or
`package_info_plus` (the channel answers the directories, version, installer and exit
reason itself), so the package adds no `UserDefaults` use and no transitive licences.

## Quick start (3 lines)

```dart
WidgetsFlutterBinding.ensureInitialized();
await StudioPulse.init(writeKey: 'sp_pub_...', endpoint: 'https://collector.example.com');
StudioPulse.levelFail(12, 'hit_spike', x: 0.42, y: 0.77, progress: 0.8);
```

`pubspec.yaml`: `studio_pulse: {git: {url: https://github.com/emirhann-akca/studio-pulse.git, path: flutter/studio_pulse}}`
(not on pub.dev; `flutter pub publish --dry-run` passes). Android minSdk 24, iOS 13.

## API

Static on `StudioPulse`; no-ops before `init`; nothing throws into the app.

| Call | Sends |
|---|---|
| `init(writeKey:, endpoint:, options:, perf: true, processStart:)` | starts everything below |
| `levelStart(level, levelName:, world:, mode:)` | `level_start` (attempts counted per level by the SDK) |
| `levelComplete(level, score:, stars:, movesLeft:)` | `level_complete` with `attempt`, `duration_ms` (foreground time) |
| `levelFail(level, reason, x:, y:, progress:, worldSpace:, score:)` | `level_fail` with the death position |
| `levelQuit(level, reason:, progress:, x:, y:)` | `level_quit` |
| `tutorialBegin()`, `tutorialStep(step, stepName:)`, `tutorialComplete()` | `tutorial_*` |
| `settingChange(setting, from, to)`, `settingToggle(setting, from:, to:)` | `setting_change` (`PulseSetting.music`, `sfx`, `haptics`, `notifications`, `language`, `quality`) |
| `earnCurrency(name, amount, item:, level:)`, `spendCurrency(...)` | GA4 virtual currency events |
| `purchase(transactionId, productId, priceMicros, currency, isRestore:, isSandbox:)` | `purchase` |
| `adImpression(adPlatform, adFormat, value, currency:, adSource:, adUnitName:, placement:, precision:)` | `ad_impression` |
| `adEvent(action, adFormat, placement:, errorCode:)` | `ad_event` |
| `screen(name, screenClass:)` | `screen_view` |
| `track(name, {params})` | a custom event (names and limits as in the Unity SDK) |
| `reportError(error, stack, fatal:)` | `pulse_error` for a handled error |
| `setUserId`, `setConsent`, `optOut`, `requestDataDeletion`, `resetInstallId`, `installId`, `stats`, `flush` | as in the Unity SDK |

## Captured automatically

The same events as the Unity SDK (install, sessions with the 30-minute background
timeout and 24-hour cap, foreground / background, heartbeat, app update, errors with the
same scrubbing and fingerprint, previous exit reason, perf and cold start, consent).
Lifecycle: `AppLifecycleListener.onStateChange` (`paused` / `hidden` → background and
flush, `resumed` → foreground, `detached` and `onExitRequested` → clean quit). Errors:
`FlutterError.onError` and `PlatformDispatcher.instance.onError`, each calling the
previous handler (Crashlytics keeps working; without one, Flutter's default report
runs). Context: app id, version, build (`versionCode` / `CFBundleVersion`),
`build_env` (`release` from `kReleaseMode`, else `debug`), install store, OS, device
model (`Build.MODEL` / `utsname.machine`), RAM, device tier, locale, time zone.

**Install store.** Android (`StudioPulsePlugin.kt`): `getInstallSourceInfo(pkg).installingPackageName`
on API 30+, `getInstallerPackageName` before, mapped like the Unity SDK. iOS
(`StudioPulsePlugin.swift`): `init` never waits for StoreKit: it starts with the receipt
file name (`sandboxReceipt` → `testflight`, else `app_store`; the simulator reports
`simulator`) and asks `AppTransaction.shared.environment` (iOS 16+) in the background;
when it answers (at most 10 s), later events carry it (`production` → `app_store`,
`sandbox` → `testflight`, `xcode` → `sideload`). **Exit reason**: Android 11+
`getHistoricalProcessExitReasons`; elsewhere the `unclean` / `crash` rules of the core.
**Restores**: the no-backup directory on Android, `isExcludedFromBackup` on the iOS marker.

## Delivery, privacy

Exactly as in the Unity SDK (same file layout under `<files dir>/studio-pulse`, same
batching, gzip, status handling, backoff, ids, `seq`, consent, opt-out, deletion and
child mode): see [SDK_UNITY.md](SDK_UNITY.md#delivery). Status codes are classified in
every build mode: unlike `aptabase_flutter`, which checks HTTP status only in debug
builds, so that in release a 5xx counts as success and the batch is deleted, this SDK
has no `kDebugMode` branch in delivery (the HTTP tests run that code path).

The iOS plug-in ships `ios/Resources/PrivacyInfo.xcprivacy` (generated from the
contract; collected types, no tracking, file timestamp API `C617.1` for the queue's own
file sizes) through the podspec's `resource_bundles`. Play Data safety: as in the Unity
SDK.

## Tests

```bash
cd flutter/studio_pulse
dart analyze && flutter test                  # 42 tests
flutter pub publish --dry-run
dart run tool/e2e_driver.dart http://127.0.0.1:8787 sp_pub_local /tmp/pulse-e2e   # against `pulse collect`
```

- `test/http_status_test.dart`: an in-process `HttpServer` on 127.0.0.1 with the
  production transport: kill mid-flush (the server stored the batch, the connection
  died; the next launch re-sends, the dedupe drops the copy, nothing is lost), 24 h
  offline replay, 413 halving, 400 drop with the reason, 401 disable until the next
  launch, 429 / 5xx / connection-reset backoff timings and `Retry-After`, gzip.
- `test/core_test.dart`: sessions, heartbeat, `seq` across restarts, update and exit
  reasons, restores, attempts, flush triggers, queue cap, torn lines, opt-out, consent,
  deletion, child mode, errors, validation, string escaping.
- `test/conformance_test.dart`: the shared script and golden (byte for byte with C#) and
  3,093 doubles against Python's shortest repr.
- `test/flutter_adapter_test.dart`: the facade with a mocked channel, real lifecycle
  transitions and error hooks, delivering to a real server.

## Not verified on a device

**Simulator smoke test (12 Nov 2026):** the example app on an iPhone 17 simulator (iOS
26.0.1) against a local `pulse collect`: the Swift channel answered version, build, OS
and RAM, `install_store` was `simulator`; launch, Home (background, which flushed),
reopen (`pulse_foreground` in the same session with `fg_ms` continuing, a partial
`pulse_perf` window) and Home again arrived as seq 1-7 with nothing quarantined.

Not run on hardware: a Flutter app depending on the package
was built on this Mac as a debug APK (Kotlin channel compiled with AGP 8.11 /
Kotlin 2.2) and as an iOS simulator app (Swift channel compiled by Xcode 26.6; the
`studio_pulse_privacy.bundle` with the manifest is inside `studio_pulse.framework`).
Still to smoke-test on devices: the Kotlin channel on Android 9 / 11 / 14 (installer,
exit reasons, no-backup directory), the Swift channel on iOS 15 / 17 (TestFlight
`sandbox`, App Store `production`, the background AppTransaction answer), and lifecycle
callbacks on real devices. Flutter web is not supported (dart:io).
