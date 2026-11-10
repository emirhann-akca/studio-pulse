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
| `ft_p50_ms` | double | ms | Median frame time of the window |
| `ft_p95_ms` | double | ms | 95th percentile frame time of the window |
| `ft_max_ms` | double | ms | Slowest frame of the window |
| `slow_frames` | int | frames | Frames slower than 33.3 ms (below 30 fps) |
| `mem_alloc_mb` | int | MB | Peak `Profiler.GetTotalAllocatedMemoryLong()`, sampled once per second |
| `mem_reserved_mb` | int | MB | Peak `Profiler.GetTotalReservedMemoryLong()` |
| `sys_mem_mb` | int | MB | `SystemInfo.systemMemorySize` |

Frame times are `Time.unscaledDeltaTime`. The first frame after resuming from
the background and any delta above 5 s are skipped. Percentiles use linear
interpolation between closest ranks over the most recent frames in the ring
buffer (4096 frames by default, i.e. a full 30 s window up to ~136 fps);
`frames` and `slow_frames` count every frame of the window.

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
{"v":1,"event":"pulse_perf","ts":"2026-10-01T12:00:30.000Z","app":"com.example.game","app_version":"1.4.0","platform":"ANDROID","session":"0123456789abcdef0123456789abcdef","params":{"pulse_v":1,"tier":"low","scene":"Level","window_s":30,"frames":1800,"ft_p50_ms":17.332,"ft_p95_ms":18.026,"ft_max_ms":50.465,"slow_frames":19,"mem_alloc_mb":412,"mem_reserved_mb":530,"sys_mem_mb":3072}}
```

| Key | Meaning |
|---|---|
| `v` | Envelope version (`1`); other versions are counted as malformed |
| `event` | `pulse_perf` or `pulse_load` |
| `ts` | UTC timestamp, ISO 8601 with milliseconds; the warehouse date is the UTC date |
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
