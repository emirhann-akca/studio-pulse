using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.SceneManagement;

namespace Studio.Pulse
{
    [Serializable]
    public sealed class PulseConfig
    {
        [Tooltip("Length of one frame-time window; one pulse_perf event per window.")]
        public float windowSeconds = 30f;
        [Tooltip("How often queued events are handed to the sinks (also on pause/quit).")]
        public float flushIntervalSeconds = 30f;
        [Tooltip("Share of sessions that record frame-time windows (0..1). Load times are always recorded.")]
        [Range(0f, 1f)] public float sessionSampleRate = 1f;
        [Tooltip("Ring buffer capacity in frames (4096 covers 30 s at 120 fps).")]
        public int ringCapacity = 4096;
        [Tooltip("Record GPU frame time on OpenGL ES too. Off by default: GPU timing queries cost GPU time there (Unity manual, FrameTimingManager). Also needs Player Settings > OpenGL: Profiler GPU Recorders.")]
        public bool recordGpuTimeOnOpenGLES = false;
        [Tooltip("Report cold start on the first frame if the game never calls Pulse.MarkInteractive().")]
        public bool autoMarkInteractive = false;
        [Tooltip("Log every flushed batch to the console (development builds).")]
        public bool logBatches = false;
    }

    /// <summary>
    /// Entry point. Call once at startup, e.g. from a bootstrap MonoBehaviour:
    /// <code>
    /// Pulse.Initialize(new PulseConfig(),
    ///     new JsonLinesSink(JsonLinesSink.DefaultFilePath())
    /// #if PULSE_FIREBASE
    ///     , new FirebaseAnalyticsSink()
    /// #endif
    /// );
    /// </code>
    /// </summary>
    public static class Pulse
    {
        static PulseRuntime _runtime;
        static readonly List<PulseEvent> Queue = new List<PulseEvent>(32);
        static readonly List<IPulseSink> Sinks = new List<IPulseSink>(2);

        public static PulseContext Context { get; private set; } = new PulseContext();
        public static PulseConfig Config { get; private set; } = new PulseConfig();
        public static bool IsInitialized { get { return _runtime != null; } }

        public static void Initialize(PulseConfig config, params IPulseSink[] sinks)
        {
            if (IsInitialized) return;
            Config = config ?? new PulseConfig();
            Sinks.Clear();
            if (sinks != null) Sinks.AddRange(sinks);
            Context = new PulseContext
            {
                AppId = Application.identifier,
                AppVersion = Application.version,
                Platform = PlatformName(Application.platform),
                Session = Guid.NewGuid().ToString("N"),
            };
            var go = new GameObject("[Studio.Pulse]") { hideFlags = HideFlags.HideInHierarchy };
            UnityEngine.Object.DontDestroyOnLoad(go);
            _runtime = go.AddComponent<PulseRuntime>();
            bool sampled = UnityEngine.Random.value < Config.sessionSampleRate;
            bool gpu = Config.recordGpuTimeOnOpenGLES || SystemInfo.graphicsDeviceType != GraphicsDeviceType.OpenGLES3;
            // Unsampled sessions create no recorders, so they pay nothing.
            _runtime.Setup(Config, sampled ? new PerfSampler(Config.windowSeconds, Config.ringCapacity, 1f, gpu) : null);
        }

        /// <summary>Call when the first screen accepts input; reports the cold start time.</summary>
        public static void MarkInteractive()
        {
            LoadTimer.MarkInteractive();
        }

        public static void Enqueue(PulseEvent e)
        {
            if (e != null) Queue.Add(e);
        }

        /// <summary>Hand all queued events to the sinks now.</summary>
        public static void Flush()
        {
            if (Queue.Count > 0)
            {
                for (int i = 0; i < Sinks.Count; i++)
                {
                    try
                    {
                        Sinks[i].Send(Queue, Context);
                    }
                    catch (Exception ex)
                    {
                        Debug.LogWarning("[Studio.Pulse] sink " + Sinks[i].GetType().Name + " failed: " + ex.Message);
                    }
                }
                if (Config.logBatches) Debug.Log("[Studio.Pulse] flushed " + Queue.Count + " event(s)");
                Queue.Clear();
            }
            for (int i = 0; i < Sinks.Count; i++) Sinks[i].Flush();
        }

        internal static string PlatformName(RuntimePlatform p)
        {
            switch (p)
            {
                case RuntimePlatform.Android: return "ANDROID";
                case RuntimePlatform.IPhonePlayer: return "IOS";
                case RuntimePlatform.WebGLPlayer: return "WEB";
                case RuntimePlatform.OSXEditor:
                case RuntimePlatform.WindowsEditor:
                case RuntimePlatform.LinuxEditor: return "EDITOR";
                default: return p.ToString().ToUpperInvariant();
            }
        }
    }

    [AddComponentMenu("")]
    [DefaultExecutionOrder(-10000)]
    internal sealed class PulseRuntime : MonoBehaviour
    {
        PulseConfig _config;
        PerfSampler _sampler;
        float _nextFlush;
        bool _firstFrame = true;

        internal void Setup(PulseConfig config, PerfSampler sampler)
        {
            _config = config;
            _sampler = sampler;
            _nextFlush = Time.unscaledTime + config.flushIntervalSeconds;
            SceneManager.sceneLoaded += LoadTimer.OnSceneLoaded;
        }

        void Update()
        {
            if (_firstFrame)
            {
                _firstFrame = false;
                if (_config.autoMarkInteractive) LoadTimer.MarkInteractive();
            }
            LoadTimer.OnFrame();
            if (_sampler != null)
            {
                PulseEvent e = _sampler.Tick(Time.unscaledDeltaTime);
                if (e != null) Pulse.Enqueue(e);
            }
            if (Time.unscaledTime >= _nextFlush)
            {
                _nextFlush = Time.unscaledTime + _config.flushIntervalSeconds;
                Pulse.Flush();
            }
        }

        void OnApplicationPause(bool paused)
        {
            if (paused)
            {
                if (_sampler != null) Pulse.Enqueue(_sampler.CloseWindow());
                Pulse.Flush();
            }
            else if (_sampler != null)
            {
                _sampler.SkipNextFrame();
            }
        }

        void OnApplicationQuit()
        {
            if (_sampler != null) Pulse.Enqueue(_sampler.CloseWindow());
            Pulse.Flush();
        }

        void OnDestroy()
        {
            SceneManager.sceneLoaded -= LoadTimer.OnSceneLoaded;
            if (_sampler != null) _sampler.Dispose();
        }
    }
}
