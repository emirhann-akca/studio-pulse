## 2.0.0

- The full analytics SDK on event contract v2: pure-Dart core (`package:studio_pulse/core.dart`)
  with an on-disk segment queue, batching (20 events / 30 s / background), gzip, exponential
  backoff with jitter, the collector's status codes (413 halves, 400 drops, 401/403 stop until
  the next launch), UUIDv7 ids, a persisted sequence number, 30-minute sessions, consent,
  opt-out, deletion requests and child mode.
- Flutter facade `StudioPulse`: AppLifecycleListener, FlutterError / PlatformDispatcher error
  hooks, install source and exit reason through a small platform channel (Android, iOS),
  iOS privacy manifest.
- Renamed from `studio_pulse_perf`; the perf API (`studio_pulse_perf.dart`) is unchanged and its
  events now also go through the SDK queue.

## 0.1.0

- Frame-time, start-time and memory telemetry (`pulse_perf`, `pulse_load`).
