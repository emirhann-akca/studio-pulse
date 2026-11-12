# studio_pulse

The studio-pulse analytics SDK for Flutter apps (event contract v2): a crash-safe
on-disk queue, batching, gzip, retries with backoff, sessions, install store, level /
economy / ad / purchase / settings events, managed errors, consent and opt-out.
Full reference: `docs/SDK_FLUTTER.md` in the repository.

```dart
WidgetsFlutterBinding.ensureInitialized();
await StudioPulse.init(writeKey: 'sp_pub_...', endpoint: 'https://collector.example.com');
StudioPulse.levelFail(12, 'hit_spike', x: 0.42, y: 0.77, progress: 0.8);
```

## Perf telemetry

The package also carries the frame-time, start-time and memory telemetry that was
`studio_pulse_perf` (`StudioPulse.init` starts it and sends its events through the SDK
queue; it can still be used on its own with Firebase or JSON-lines sinks):

```dart
import 'package:studio_pulse/studio_pulse_perf.dart';

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

Checks: `dart analyze` and `flutter test` (42 tests, of which 8 for perf; the golden JSON in
`test/golden/` is also parsed by the Python connector's tests).
