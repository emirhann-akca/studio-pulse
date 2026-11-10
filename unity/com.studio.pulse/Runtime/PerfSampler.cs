using System;
using Unity.Profiling;
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
    /// Records one frame time per frame into a ring buffer and emits one
    /// pulse_perf event per window (default 30 s).
    ///
    /// Sources (each event says which one it used, see ft_src / mem_src):
    /// - Frame time: FrameTimingManager's "CPU Total Frame Time" through a
    ///   ProfilerRecorder (time between the ends of two frames, vsync waits
    ///   included, Application.Pause excluded). With a recorder attached the
    ///   FrameTimingManager records only the counters asked for, and the "Frame
    ///   Timing Stats" player setting is not needed. Where the counter reports
    ///   nothing (older Unity, unsupported platform) the window falls back to
    ///   Time.unscaledDeltaTime.
    /// - "CPU Main Thread Frame Time" and "GPU Frame Time": per-window p95, to tell
    ///   a CPU-bound window from a GPU-bound one. GPU timing is off on OpenGL ES by
    ///   default (PulseConfig.recordGpuTimeOnOpenGLES): measuring it there costs GPU
    ///   time.
    /// - Memory: the "Total Reserved Memory", "GC Reserved Memory" and "System Used
    ///   Memory" counters, documented as available in release players;
    ///   Profiler.GetTotalReservedMemoryLong / GetMonoHeapSizeLong when a counter is
    ///   not valid. System Used Memory (what the OS sees, the number low-memory
    ///   kills act on) has no API fallback and is omitted when unavailable.
    ///
    /// Per frame: up to three ProfilerRecorder.LastValue reads and three float
    /// stores into preallocated rings; no allocations. Memory is read once per
    /// second. Sorting and the event object happen once per window.
    /// </summary>
    public sealed class PerfSampler : IDisposable
    {
        const float BytesPerMb = 1024f * 1024f;
        const float NsPerMs = 1000000f; // ProfilerRecorder time values are nanoseconds
        const float MaxPlausibleFrameSeconds = 5f;

        readonly FrameWindow _window;
        readonly FrameWindow _cpuMain;
        readonly FrameWindow _gpu;
        readonly float _windowSeconds;
        readonly float _memorySampleSeconds;
        readonly bool _recordGpu;

        // Not readonly: ProfilerRecorder is a struct that Dispose() must reach.
        ProfilerRecorder _frameTime;
        ProfilerRecorder _cpuMainTime;
        ProfilerRecorder _gpuTime;
        ProfilerRecorder _totalReserved;
        ProfilerRecorder _gcReserved;
        ProfilerRecorder _systemUsed;
        readonly bool _hasCpuMain;
        readonly bool _hasGpu;
        readonly bool _hasTotalReserved;
        readonly bool _hasGcReserved;
        readonly bool _hasSystemUsed;

        bool _windowUsesFtm;
        bool _memFromRecorder;
        float _elapsed;
        float _sinceMemorySample;
        long _peakAllocated;
        long _peakReserved;
        long _peakGcReserved;
        long _peakSystemUsed;
        bool _skipNext = true;
        bool _disposed;

        public PerfSampler(float windowSeconds = 30f, int ringCapacity = 4096, float memorySampleSeconds = 1f, bool recordGpuTime = true)
        {
            _windowSeconds = Mathf.Max(5f, windowSeconds);
            _memorySampleSeconds = Mathf.Max(0.25f, memorySampleSeconds);
            _window = new FrameWindow(ringCapacity);
            _cpuMain = new FrameWindow(ringCapacity);
            _gpu = new FrameWindow(ringCapacity);
            _recordGpu = recordGpuTime;

            // Counter names and categories as in the Unity 6.3 manual ("Frame timing
            // API counter reference", "Accessing memory counters in players").
            _frameTime = ProfilerRecorder.StartNew(ProfilerCategory.Internal, "CPU Total Frame Time");
            _cpuMainTime = ProfilerRecorder.StartNew(ProfilerCategory.Internal, "CPU Main Thread Frame Time");
            if (_recordGpu) _gpuTime = ProfilerRecorder.StartNew(ProfilerCategory.Internal, "GPU Frame Time");
            _totalReserved = ProfilerRecorder.StartNew(ProfilerCategory.Memory, "Total Reserved Memory");
            _gcReserved = ProfilerRecorder.StartNew(ProfilerCategory.Memory, "GC Reserved Memory");
            _systemUsed = ProfilerRecorder.StartNew(ProfilerCategory.Memory, "System Used Memory");

            _hasCpuMain = _cpuMainTime.Valid;
            _hasGpu = _recordGpu && _gpuTime.Valid;
            _hasTotalReserved = _totalReserved.Valid;
            _hasGcReserved = _gcReserved.Valid;
            _hasSystemUsed = _systemUsed.Valid;
            ResetWindow();
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
            float frameMs = unscaledDeltaTime * 1000f;
            if (_windowUsesFtm)
            {
                long ns = _frameTime.LastValue;
                if (ns > 0) frameMs = ns / NsPerMs;
            }
            _window.Add(frameMs);
            if (_hasCpuMain)
            {
                long ns = _cpuMainTime.LastValue;
                if (ns > 0) _cpuMain.Add(ns / NsPerMs);
            }
            if (_hasGpu)
            {
                long ns = _gpuTime.LastValue;
                if (ns > 0) _gpu.Add(ns / NsPerMs);
            }
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
            long reserved = _hasTotalReserved ? _totalReserved.LastValue : 0;
            _memFromRecorder = reserved > 0;
            if (!_memFromRecorder) reserved = Profiler.GetTotalReservedMemoryLong();
            long gcReserved = _hasGcReserved ? _gcReserved.LastValue : 0;
            if (gcReserved <= 0) gcReserved = Profiler.GetMonoHeapSizeLong();
            long systemUsed = _hasSystemUsed ? _systemUsed.LastValue : 0;
            long allocated = Profiler.GetTotalAllocatedMemoryLong();
            if (allocated > _peakAllocated) _peakAllocated = allocated;
            if (reserved > _peakReserved) _peakReserved = reserved;
            if (gcReserved > _peakGcReserved) _peakGcReserved = gcReserved;
            if (systemUsed > _peakSystemUsed) _peakSystemUsed = systemUsed;
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
                .AddText(PulseSchema.ParamFtSource, _windowUsesFtm ? PulseSchema.SourceFrameTiming : PulseSchema.SourceDeltaTime)
                .AddDouble(PulseSchema.ParamFtP50, p50)
                .AddDouble(PulseSchema.ParamFtP95, p95)
                .AddDouble(PulseSchema.ParamFtMax, _window.MaxMs)
                .AddInt(PulseSchema.ParamSlowFrames, _window.SlowFrames);
            // CPU / GPU p95 only when they cover at least half of the window's
            // frames; otherwise they would describe a handful of frames.
            if (_cpuMain.Frames * 2 >= _window.Frames) e.AddDouble(PulseSchema.ParamCpuMainP95, P95(_cpuMain));
            if (_gpu.Frames * 2 >= _window.Frames) e.AddDouble(PulseSchema.ParamGpuP95, P95(_gpu));
            e.AddText(PulseSchema.ParamMemSource, _memFromRecorder ? PulseSchema.SourceRecorder : PulseSchema.SourceProfilerApi)
                .AddInt(PulseSchema.ParamMemAlloc, (long)(_peakAllocated / BytesPerMb))
                .AddInt(PulseSchema.ParamMemReserved, (long)(_peakReserved / BytesPerMb))
                .AddInt(PulseSchema.ParamGcReserved, (long)(_peakGcReserved / BytesPerMb));
            if (_peakSystemUsed > 0) e.AddInt(PulseSchema.ParamSysUsed, (long)(_peakSystemUsed / BytesPerMb));
            e.AddInt(PulseSchema.ParamSysMem, SystemInfo.systemMemorySize);
            ResetWindow();
            return e;
        }

        static float P95(FrameWindow w)
        {
            float p50, p95;
            w.Snapshot(out p50, out p95);
            return p95;
        }

        void ResetWindow()
        {
            _window.Reset();
            _cpuMain.Reset();
            _gpu.Reset();
            _elapsed = 0f;
            _sinceMemorySample = 0f;
            _peakAllocated = 0;
            _peakReserved = 0;
            _peakGcReserved = 0;
            _peakSystemUsed = 0;
            // One frame-time source per window. FrameTimingManager results arrive a
            // few frames late, so a session's first window uses unscaledDeltaTime.
            _windowUsesFtm = !_disposed && _frameTime.Valid && _frameTime.LastValue > 0;
        }

        /// <summary>Releases the profiler recorders (called when the runtime is destroyed).</summary>
        public void Dispose()
        {
            if (_disposed) return;
            _disposed = true;
            _frameTime.Dispose();
            _cpuMainTime.Dispose();
            if (_recordGpu) _gpuTime.Dispose();
            _totalReserved.Dispose();
            _gcReserved.Dispose();
            _systemUsed.Dispose();
        }
    }
}
