# Studio Pulse (Unity package)

Client performance telemetry for mobile games, in the event schema the
[studio-pulse](../../README.md) warehouse reads:

- **PerfSampler** - frame time of every frame (`Time.unscaledDeltaTime`) in a
  ring buffer; one `pulse_perf` event per 30 s window with p50 / p95 / max
  frame time, the number of frames slower than 33.3 ms, and peak allocated /
  reserved memory (`Profiler.GetTotalAllocatedMemoryLong` /
  `GetTotalReservedMemoryLong`).
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

Per frame: one float store into a preallocated ring buffer, a few comparisons
and additions, and a boolean check for pending scene loads - no allocations
(`tests/test_event_schema.py` checks the per-frame methods statically, and the
Mono harness measured 0 bytes allocated across 100 000 `FrameWindow.Add`
calls). Memory is read once per second. Sorting the window (at most 4096
floats), building the event and serialising it happen once per window.

## Config

| Field | Default | Meaning |
|---|---|---|
| `windowSeconds` | 30 | Length of a frame-time window |
| `flushIntervalSeconds` | 30 | How often queued events go to the sinks |
| `sessionSampleRate` | 1 | Share of sessions that record frame times (loads are always recorded) |
| `ringCapacity` | 4096 | Frames kept for percentiles (a full window up to ~136 fps) |
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
