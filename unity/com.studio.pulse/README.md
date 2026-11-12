# Studio Pulse (Unity package)

**Analytics SDK** (event contract v2, full reference in `docs/SDK_UNITY.md`):

```csharp
[RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
static void Boot() => Pulse.Init("sp_pub_...", "https://collector.example.com");
Pulse.LevelFail(12, "hit_spike", new Vector2(0.42f, 0.77f), progress: 0.8f);
```

Crash-safe disk queue, batching, gzip, backoff, sessions, install store (Android
installer through JNI, iOS AppTransaction through `Plugins/iOS`), exit reasons,
managed errors, level / economy / ad / purchase / settings helpers, consent, opt-out,
deletion requests, child mode, `PrivacyInfo.xcprivacy`. The core
(`Runtime/Core`, assembly `Studio.Pulse.Core`) has no UnityEngine reference. The
perf telemetry below is started by `Pulse.Init` and sent through the same queue.

## Perf telemetry

Client performance telemetry for mobile games, in the event schema the
[studio-pulse](../../README.md) warehouse reads:

- **PerfSampler** - one `pulse_perf` event per 30 s window with p50 / p95 / max
  frame time, the number of frames slower than 33.3 ms, CPU main-thread and
  GPU p95, and peak memory. It reads Unity's release-player APIs through
  `ProfilerRecorder`: FrameTimingManager's "CPU Total Frame Time", "CPU Main
  Thread Frame Time" and "GPU Frame Time", and the "Total Reserved Memory",
  "GC Reserved Memory" and "System Used Memory" counters. When a counter is
  not valid it falls back to `Time.unscaledDeltaTime` / the `Profiler` API, and
  the event records which source it used (`ft_src`, `mem_src`).
- **LoadTimer** - cold start (engine start to first interactive frame) and
  scene load times as `pulse_load` events.
- **Device tier** - `low` / `mid` / `high` from `SystemInfo.systemMemorySize`,
  `processorCount` and `graphicsMemorySize` (rules in `docs/EVENTS.md`).
- **Sinks** - `JsonLinesSink` (local file and/or HTTP POST of JSON lines) and
  `FirebaseAnalyticsSink` (Firebase Analytics, compiled only with the
  `PULSE_FIREBASE` define). Implement `IPulseSink` for anything else.

Events are batched and handed to the sinks every 30 s and when the app is
paused or quits.

## Install

Unity Package Manager -> *Add package from git URL*:

```
https://github.com/emirhann-akca/studio-pulse.git?path=/unity/com.studio.pulse
```

or copy `unity/com.studio.pulse` into your project's `Packages/` folder.
Requires Unity 2021.3 or newer.

## Use

```csharp
using Studio.Pulse;
using UnityEngine;

public class Bootstrap : MonoBehaviour
{
    void Awake()
    {
        Pulse.Initialize(
            new PulseConfig { windowSeconds = 30f, sessionSampleRate = 0.5f },
            new JsonLinesSink(JsonLinesSink.DefaultFilePath())
#if PULSE_FIREBASE
            , new FirebaseAnalyticsSink()
#endif
        );
    }
}

// When the first screen accepts input:
Pulse.MarkInteractive();            // -> pulse_load kind=cold_start

// Timed scene loads:
LoadTimer.LoadSceneAsync("Level");  // -> pulse_load kind=scene_load
```

`PULSE_FIREBASE` is defined automatically (asmdef *Version Defines*) when the
`com.google.firebase.analytics` UPM package is installed. If you imported
Firebase as a `.unitypackage`, add `PULSE_FIREBASE` to *Player Settings ->
Scripting Define Symbols* yourself.

`JsonLinesSink(filePath, httpEndpoint, authHeader)`: the file is append-only
(`Application.persistentDataPath/pulse/events.jsonl` by default); the HTTP
endpoint receives `POST` bodies of `application/x-ndjson`. Failed uploads are
kept (up to 256 KB) and retried on the next flush. There is no collector in
this repository - any endpoint that appends the lines to storage works, and
the `unity_jsonl` source of studio-pulse reads the resulting files.

## Overhead

Per frame: up to three `ProfilerRecorder.LastValue` reads, three float stores
into preallocated ring buffers and a boolean check for pending scene loads - no
allocations (the compiled IL of `PerfSampler.Tick` contains no `newobj`,
`newarr` or `box`; `tests/test_event_schema.py` also checks the per-frame
methods statically, and the Mono harness measured 0 bytes allocated across
100 000 `FrameWindow.Add` calls). Memory is read once per second. Sorting the
windows (at most 3 x 4096 floats), building the event and serialising it
happen once per window. Sessions outside `sessionSampleRate` create no
recorders at all.

FrameTimingManager is off by default in release builds because GPU timing has
a cost on some graphics APIs (OpenGL ES). Attaching a `ProfilerRecorder` to a
specific counter makes it record only that counter, so the "Frame Timing
Stats" player setting is not required; GPU time is not requested on OpenGL ES
unless `recordGpuTimeOnOpenGLES` is set.

## Config

| Field | Default | Meaning |
|---|---|---|
| `windowSeconds` | 30 | Length of a frame-time window |
| `flushIntervalSeconds` | 30 | How often queued events go to the sinks |
| `sessionSampleRate` | 1 | Share of sessions that record frame times (loads are always recorded) |
| `ringCapacity` | 4096 | Frames kept for percentiles (a full window up to ~136 fps) |
| `recordGpuTimeOnOpenGLES` | false | Also record GPU frame time on OpenGL ES (costs GPU time there) |
| `autoMarkInteractive` | false | Report cold start at the first frame if `MarkInteractive()` is never called |
| `logBatches` | false | Log each flush to the console |

## Privacy

No user id, device id or advertising id is sent by this package. The JSON-lines
envelope carries a random per-launch session id, the bundle id, the app
version and the platform. With the Firebase sink, Firebase's own identifiers
apply as for any other Analytics event.

## Layout

The files without `UnityEngine` references (`PulseSchema`, `PulseEvent`,
`PulseJson`, `FrameWindow`, `DeviceTierRules`) can be compiled and run outside
Unity; the repository's tests use output produced that way.
