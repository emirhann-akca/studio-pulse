# Client event schema

This is the contract between the game client and the warehouse. The Unity
package (`unity/com.studio.pulse`) emits exactly these events; the Python
connectors read exactly these names. The constants live in two places that a
test keeps in sync:

- C#: `unity/com.studio.pulse/Runtime/PulseSchema.cs`
- Python: `src/studio_pulse/events.py`

`tests/test_event_schema.py` fails if they drift apart, and also parses a
sample produced by running the package's serializer under Mono
(`tests/fixtures/unity/csharp_sample.jsonl`).

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
| `ft_src` | string | - | Where the frame times came from: `ftm` or `dt` (below) |
| `ft_p50_ms` | double | ms | Median frame time of the window |
| `ft_p95_ms` | double | ms | 95th percentile frame time of the window |
| `ft_max_ms` | double | ms | Slowest frame of the window |
| `slow_frames` | int | frames | Frames slower than 33.3 ms (below 30 fps) |
| `cpu_main_p95_ms` | double | ms | p95 of FrameTimingManager "CPU Main Thread Frame Time" (main thread work, vsync and render-thread waits excluded). Optional |
| `gpu_p95_ms` | double | ms | p95 of FrameTimingManager "GPU Frame Time". Optional; off on OpenGL ES by default |
| `mem_src` | string | - | `recorder` (ProfilerRecorder memory counters) or `profiler_api` (fallback) |
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
