using System;
using UnityEngine;
using UnityEngine.Profiling;
using UnityEngine.SceneManagement;

namespace Studio.Pulse
{
    /// <summary>Device tier of the current device (see DeviceTierRules).</summary>
    public static class DeviceTier
    {
        static string _cached;

        public static string Current
        {
            get
            {
                if (_cached == null)
                {
                    _cached = DeviceTierRules.Classify(SystemInfo.systemMemorySize, SystemInfo.processorCount, SystemInfo.graphicsMemorySize);
                }
                return _cached;
            }
        }
    }

    /// <summary>
    /// Samples Time.unscaledDeltaTime every frame into a ring buffer and emits one
    /// pulse_perf event per window (default 30 s): p50/p95/max frame time, number
    /// of frames slower than 33.3 ms, peak allocated/reserved memory.
    ///
    /// Per-frame cost: one float store, a few integer/float operations and a
    /// comparison; no allocations. Memory is read once per second. Sorting and the
    /// event object happen once per window.
    /// </summary>
    public sealed class PerfSampler
    {
        const float BytesPerMb = 1024f * 1024f;
        const float MaxPlausibleFrameSeconds = 5f;

        readonly FrameWindow _window;
        readonly float _windowSeconds;
        readonly float _memorySampleSeconds;
        float _elapsed;
        float _sinceMemorySample;
        long _peakAllocated;
        long _peakReserved;
        bool _skipNext = true;

        public PerfSampler(float windowSeconds = 30f, int ringCapacity = 4096, float memorySampleSeconds = 1f)
        {
            _windowSeconds = Mathf.Max(5f, windowSeconds);
            _memorySampleSeconds = Mathf.Max(0.25f, memorySampleSeconds);
            _window = new FrameWindow(ringCapacity);
        }

        /// <summary>Skip the next delta (e.g. after resuming from background).</summary>
        public void SkipNextFrame()
        {
            _skipNext = true;
        }

        /// <summary>Call once per frame. Returns a finished event when a window closes, else null.</summary>
        public PulseEvent Tick(float unscaledDeltaTime)
        {
            if (_skipNext || unscaledDeltaTime <= 0f || unscaledDeltaTime > MaxPlausibleFrameSeconds)
            {
                _skipNext = false;
                return null;
            }
            _window.Add(unscaledDeltaTime * 1000f);
            _elapsed += unscaledDeltaTime;
            _sinceMemorySample += unscaledDeltaTime;
            if (_sinceMemorySample >= _memorySampleSeconds)
            {
                _sinceMemorySample = 0f;
                SampleMemory();
            }
            return _elapsed >= _windowSeconds ? CloseWindow() : null;
        }

        void SampleMemory()
        {
            long allocated = Profiler.GetTotalAllocatedMemoryLong();
            long reserved = Profiler.GetTotalReservedMemoryLong();
            if (allocated > _peakAllocated) _peakAllocated = allocated;
            if (reserved > _peakReserved) _peakReserved = reserved;
        }

        /// <summary>Close the current window early (pause/quit). Null when it holds too few frames.</summary>
        public PulseEvent CloseWindow(int minFrames = 30)
        {
            if (_window.Frames < minFrames)
            {
                ResetWindow();
                return null;
            }
            SampleMemory();
            float p50, p95;
            _window.Snapshot(out p50, out p95);
            var e = new PulseEvent(PulseSchema.PerfEvent, PulseJson.ToUnixMs(DateTime.UtcNow))
                .AddText(PulseSchema.ParamTier, DeviceTier.Current)
                .AddText(PulseSchema.ParamScene, SceneManager.GetActiveScene().name)
                .AddDouble(PulseSchema.ParamWindowS, _elapsed)
                .AddInt(PulseSchema.ParamFrames, _window.Frames)
                .AddDouble(PulseSchema.ParamFtP50, p50)
                .AddDouble(PulseSchema.ParamFtP95, p95)
                .AddDouble(PulseSchema.ParamFtMax, _window.MaxMs)
                .AddInt(PulseSchema.ParamSlowFrames, _window.SlowFrames)
                .AddInt(PulseSchema.ParamMemAlloc, (long)(_peakAllocated / BytesPerMb))
                .AddInt(PulseSchema.ParamMemReserved, (long)(_peakReserved / BytesPerMb))
                .AddInt(PulseSchema.ParamSysMem, SystemInfo.systemMemorySize);
            ResetWindow();
            return e;
        }

        void ResetWindow()
        {
            _window.Reset();
            _elapsed = 0f;
            _sinceMemorySample = 0f;
            _peakAllocated = 0;
            _peakReserved = 0;
        }
    }
}
