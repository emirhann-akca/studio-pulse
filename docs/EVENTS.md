# Client event schema

This is the contract between the game client and the warehouse. The Unity
package (`unity/com.studio.pulse`) and the Flutter package
(`flutter/studio_pulse_perf`) emit exactly these events; the Python connectors
read exactly these names. The constants live in three places that tests keep
in sync:

- C#: `unity/com.studio.pulse/Runtime/PulseSchema.cs`
- Dart: `flutter/studio_pulse_perf/lib/src/schema.dart`
- Python: `src/studio_pulse/events.py`

`tests/test_event_schema.py` fails if C# and Python drift apart, and also
parses a sample produced by running the package's serializer under Mono
(`tests/fixtures/unity/csharp_sample.jsonl`). `tests/test_flutter_package.py`
does the same for Dart: the constants, and the JSON lines that the package's
own `flutter test` writes (`flutter/studio_pulse_perf/test/golden/pulse_sample.jsonl`).

## Transports

The same event can reach the warehouse in two ways:

| Transport | Sink in the Unity package | Lands in | Read by |
|---|---|---|---|
| Firebase Analytics | `FirebaseAnalyticsSink` (compiled when `PULSE_FIREBASE` is defined) | GA4 BigQuery export, `events_YYYYMMDD.event_params` | `ga4_bigquery` source |
| JSON lines | `JsonLinesSink` (local file and/or HTTP POST, `application/x-ndjson`) | `.jsonl` files you collect | `unity_jsonl` source |

GA4 limits apply to both, so the schema stays inside them: event and parameter
names are at most 40 characters (`[a-z][a-z0-9_]*`), at most 25 parameters per
event, string values well under 100 characters.

## `pulse_perf` - one frame-time window

Emitted once per window (default 30 s of unscaled time) and when the app is
paused or quits (a partial window is sent only if it has at least 30 frames).
Sessions can be sampled with `PulseConfig.sessionSampleRate`.

| Parameter | Type | Unit | Meaning |
|---|---|---|---|
| `pulse_v` | int | - | Schema version, currently `1` |
| `tier` | string | - | Device tier: `low`, `mid` or `high` (rules below) |
| `scene` | string | - | Active scene when the window closed |
| `window_s` | double | s | Window length actually covered |
| `frames` | int | frames | Frames rendered in the window |
| `ft_src` | string | - | Where the frame times came from: `ftm` or `dt` (Unity, below), `flutter_ft` (Flutter) |
| `ft_p50_ms` | double | ms | Median frame time of the window |
| `ft_p95_ms` | double | ms | 95th percentile frame time of the window |
| `ft_max_ms` | double | ms | Slowest frame of the window |
| `slow_frames` | int | frames | Frames slower than 33.3 ms (below 30 fps) |
| `janky_frames` | int | frames | Frames longer than the display's frame budget (16.7 ms at 60 Hz). Optional; sent by the Flutter package |
| `cpu_main_p95_ms` | double | ms | p95 of FrameTimingManager "CPU Main Thread Frame Time" (main thread work, vsync and render-thread waits excluded). Optional |
| `gpu_p95_ms` | double | ms | p95 of FrameTimingManager "GPU Frame Time". Optional; off on OpenGL ES by default |
| `mem_src` | string | - | `recorder` (ProfilerRecorder memory counters) or `profiler_api` (fallback); `dart_rss` (Flutter) |
| `mem_alloc_mb` | int | MB | Peak `Profiler.GetTotalAllocatedMemoryLong()`, sampled once per second |
| `mem_reserved_mb` | int | MB | Peak "Total Reserved Memory" counter (fallback `Profiler.GetTotalReservedMemoryLong()`) |
| `gc_reserved_mb` | int | MB | Peak "GC Reserved Memory" counter (fallback `Profiler.GetMonoHeapSizeLong()`) |
| `sys_used_mb` | int | MB | Peak "System Used Memory" counter: what the OS counts against the process. Optional (no API fallback) |
| `sys_mem_mb` | int | MB | `SystemInfo.systemMemorySize` (device RAM) |

### Where the numbers come from

The sampler uses the runtime APIs Unity documents for release players and says
which one it used, so a dashboard never silently mixes two definitions:

- **Frame time (`ft_src`).** `ftm`: FrameTimingManager's "CPU Total Frame Time"
  read through a `ProfilerRecorder` (`ProfilerCategory.Internal`), i.e. the
  time between the ends of two frames, vsync waits included and
  `Application.Pause` excluded. Attaching a recorder makes FrameTimingManager
  record just that counter, so the "Frame Timing Stats" player setting is not
  needed. FrameTimingManager results lag by a few frames, so the first window of
  a session, and every window on a platform where the counter reports nothing,
  uses `dt`: `Time.unscaledDeltaTime`. The source is fixed per window.
- **CPU vs GPU.** "CPU Main Thread Frame Time" and "GPU Frame Time" are recorded
  the same way and reported as window p95s. Next to `ft_p95_ms` they show the
  bottleneck: a GPU p95 close to the frame p95 means the GPU is the limit. A
  value is sent only when it covers at least half of the window's frames. GPU
  timing is skipped on OpenGL ES unless `PulseConfig.recordGpuTimeOnOpenGLES` is
  set, because Unity notes that measuring it there costs GPU time (it also
  needs Player Settings > "OpenGL: Profiler GPU Recorders").
- **Memory (`mem_src`).** "Total Reserved Memory", "GC Reserved Memory" and
  "System Used Memory" (`ProfilerCategory.Memory`), which the Unity manual
  lists as available in release players. If a counter is not valid the
  sampler falls back to the `Profiler` API; System Used Memory has no
  equivalent and is then omitted.

The first frame after resuming from the background and any delta above 5 s are
skipped. Percentiles use linear interpolation between closest ranks over the
most recent frames in the ring buffer (4096 frames by default, i.e. a full 30 s
window up to ~136 fps); `frames` and `slow_frames` count every frame of the
window. Per frame the sampler does up to three `ProfilerRecorder.LastValue`
reads and three stores into preallocated rings, nothing that allocates (the
compiled IL of `PerfSampler.Tick` has no `newobj`, `newarr` or `box`).

## Flutter package (`flutter/studio_pulse_perf`)

The same `pulse_perf` / `pulse_load` events from a Flutter app, for the
camera-heavy fitness app as much as for games:

```dart
final perfStart = PulsePerf.markProcessStart();     // first line of main()
WidgetsFlutterBinding.ensureInitialized();
final perf = PulsePerf(
  sinks: [FirebaseAnalyticsSink(FirebaseAnalytics.instance.logEvent)],
  processStart: perfStart,
  deviceMemoryMb: ramMb,                           // e.g. from device_info_plus; optional
)..start();
perf.scene = 'Workout';                            // reported with the next window
// on AppLifecycleState.paused: await perf.flush();
```

- **Frame times** come from `SchedulerBinding.addTimingsCallback`, which the
  engine calls with batches of `FrameTiming`. Per frame the callback reads five
  integer timestamps and stores three durations in preallocated typed lists
  (no objects beyond the list the engine passes in; a static guard in both test
  suites checks the per-frame code): total span (vsync start to raster finish,
  `FrameTiming.totalSpan`), UI-thread build time and raster-thread time.
- Per 30 s window: `ft_p50_ms`, `ft_p95_ms`, `ft_max_ms` of the total span,
  `slow_frames` (> 33.3 ms), `janky_frames` (> the frame budget, `1000 /
  refreshRateHz` ms), `cpu_main_p95_ms` = UI-thread build p95 and
  `gpu_p95_ms` = raster-thread p95. Flutter does not expose GPU time; the
  raster thread is where the frame is submitted to the GPU. `ft_src` is
  `flutter_ft` for these windows, so dashboards never mix them with Unity's
  FrameTimingManager numbers.
- **Memory**: `sys_used_mb` = peak `ProcessInfo.currentRss` (dart:io, the
  resident set the OS counts), sampled once per second, `mem_src` = `dart_rss`.
  The Dart heap is not readable in release builds, so `mem_alloc_mb`,
  `mem_reserved_mb` and `gc_reserved_mb` are not sent.
- **Cold start** (`pulse_load`, `cold_start`): from `markProcessStart()` to the
  wall-clock raster finish of the first frame (`FrameTiming`
  `rasterFinishWallTime`). Engine start-up before `main()` is not included.
- **Device tier**: the Unity rules (below). Pure Dart cannot read device RAM;
  pass `deviceMemoryMb`, otherwise the tier is `low` (4 or fewer processors)
  or `unknown`.
- **Sinks**: `FirebaseAnalyticsSink` takes the plugin's `logEvent` function,
  so the package itself depends on nothing but Flutter; `JsonLinesSink` writes
  the envelope below to a file; `MemorySink` for tests. The events are within
  GA4's limits (at most 16 parameters, names under 40 characters).

The GA4 connector maps these events into `stg_perf_windows` with no change
(`tests/test_flutter_package.py` runs the golden window through it), and
`fct_perf_daily` / `fct_perf_by_tier` add `janky_frame_share`.

## `pulse_load` - a load time

| Parameter | Type | Unit | Meaning |
|---|---|---|---|
| `pulse_v` | int | - | Schema version |
| `tier` | string | - | Device tier |
| `kind` | string | - | `cold_start` or `scene_load` |
| `scene` | string | - | Scene that became interactive / was loaded |
| `duration_ms` | double | ms | Duration (see below) |

- `cold_start`: engine start (`Time.realtimeSinceStartupAsDouble = 0`) until the
  game calls `Pulse.MarkInteractive()`, or until the first frame when
  `autoMarkInteractive` is on. Reported once per process. Time spent by the OS
  before the engine starts is not included.
- `scene_load`: from `LoadTimer.BeginSceneLoad()` / `LoadTimer.LoadSceneAsync()`
  to the first frame rendered after that scene's `sceneLoaded` callback.

## Device tier

Computed once per session from `SystemInfo` (`DeviceTierRules.Classify`):

| Tier | Rule |
|---|---|
| `low` | `systemMemorySize < 3500` MB, or `processorCount <= 4`, or a reported `graphicsMemorySize < 512` MB |
| `high` | `systemMemorySize >= 7000` MB and `processorCount >= 6` (and `graphicsMemorySize >= 2048` MB when reported) |
| `mid` | everything else |

The thresholds are a starting point for 2024-2026 phones; adjust them in one
place if your audience differs. Rows without a tier are reported as `unknown`.

## JSON-lines envelope

One event per line, UTF-8, `\n` terminated:

```json
{"v":1,"event":"pulse_perf","ts":"2026-10-01T12:00:30.000Z","app":"com.example.game","app_version":"1.4.0","platform":"ANDROID","session":"0123456789abcdef0123456789abcdef","params":{"pulse_v":1,"tier":"low","scene":"Level","window_s":30,"frames":1800,"ft_src":"ftm","ft_p50_ms":17.332,"ft_p95_ms":18.026,"ft_max_ms":50.465,"slow_frames":19,"cpu_main_p95_ms":9.914,"gpu_p95_ms":14.421,"mem_src":"recorder","mem_alloc_mb":412,"mem_reserved_mb":530,"gc_reserved_mb":96,"sys_used_mb":688,"sys_mem_mb":3072}}
```

| Key | Meaning |
|---|---|
| `v` | Envelope version (`1`); other versions are counted as malformed |
| `event` | `pulse_perf` or `pulse_load` |
| `ts` | UTC timestamp, ISO 8601 with milliseconds; the warehouse date is its calendar day in the app's configured `timezone` |
| `app` | `Application.identifier` (bundle id / package name) |
| `app_version` | `Application.version` |
| `platform` | `ANDROID`, `IOS`, `WEB`, `EDITOR` or the upper-cased `RuntimePlatform` |
| `session` | Random id per app launch (no user or device identifier is sent) |
| `params` | The parameters listed above |

## Events the game logs itself (read from GA4)

The Unity package does not emit these, but the GA4 connector understands them:

| Event | Parameters | Used for |
|---|---|---|
| `level_start`, `level_complete`, `level_fail` | `level` (int) | Level funnel and churn by level |
| `level_end` (GA4 recommended) | `level_name` (numeric), `success` (1/0 or true/false) | Same, success = complete |
| `in_app_purchase` (automatic) or your purchase event | value + currency, so GA4 fills `event_value_in_usd` | GA4-side IAP revenue, payers, LTV when RevenueCat is not connected |
| `ad_impression` | `value`, `currency` (GA4 fills `event_value_in_usd`) | Ad revenue per cohort (LTV) |
| `app_exception` (logged by Crashlytics) | `fatal` = 1 | Crash-free users/sessions when no Crashlytics export is connected |
| any custom event with an FPS parameter | configurable (`fps_events`) | Mapped onto the perf model as `ft_p50_ms = 1000 / fps`, tier `unknown` |

The list of IAP and ad events, the app ids to keep (`app_info.id`) and the FPS
mappings are configurable per app in the config file.
