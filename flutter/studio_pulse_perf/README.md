# studio_pulse_perf

Flutter performance telemetry on the studio-pulse event contract
(`docs/EVENTS.md` in the repository root): one `pulse_perf` event per 30 s
window and a `pulse_load` cold start, to Firebase Analytics (then the GA4
BigQuery export) or JSON lines.

```dart
import 'package:studio_pulse_perf/studio_pulse_perf.dart';

final perfStart = PulsePerf.markProcessStart();

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Firebase.initializeApp();
  final perf = PulsePerf(
    sinks: [FirebaseAnalyticsSink(FirebaseAnalytics.instance.logEvent)],
    processStart: perfStart,
    deviceMemoryMb: null, // pass device RAM (e.g. device_info_plus) for low/mid/high tiers
  )..start();
  runApp(MyApp(perf: perf)); // set perf.scene per screen; await perf.flush() on pause
}
```

What a window holds: frame total time (vsync start to raster finish) p50 /
p95 / max, frames over 33.3 ms and over the display's frame budget, UI-thread
build p95 (`cpu_main_p95_ms`) and raster-thread p95 (`gpu_p95_ms`; Flutter
exposes no GPU time), peak resident memory (`sys_used_mb`), device tier.
`ft_src` is `flutter_ft` and `mem_src` `dart_rss`, so these windows are never
mixed with Unity's numbers. The timings callback stores integers in
preallocated typed lists; it creates nothing per frame.

Checks: `dart analyze` and `flutter test` (8 tests; the golden JSON in
`test/golden/` is also parsed by the Python connector's tests).
