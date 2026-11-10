using UnityEngine;
using UnityEngine.SceneManagement;

namespace Studio.Pulse
{
    /// <summary>
    /// Cold start and scene load timing.
    ///
    /// Cold start: from engine start (Time.realtimeSinceStartupAsDouble = 0) to the
    /// moment the game calls MarkInteractive() - typically when the main menu
    /// accepts input - or, with PulseConfig.autoMarkInteractive, to the first
    /// rendered frame after initialisation. Reported once per process.
    ///
    /// Scene load: from BeginSceneLoad()/LoadSceneAsync() to the first frame
    /// rendered after the scene's sceneLoaded callback.
    /// </summary>
    public static class LoadTimer
    {
        static bool _coldStartReported;
        static double _sceneStart = -1;
        static string _pendingScene;
        static bool _awaitFrame;

        internal static void Reset()
        {
            _coldStartReported = false;
            _sceneStart = -1;
            _pendingScene = null;
            _awaitFrame = false;
        }

        /// <summary>Report cold start now (first call per process wins).</summary>
        public static void MarkInteractive()
        {
            if (_coldStartReported) return;
            _coldStartReported = true;
            double ms = Time.realtimeSinceStartupAsDouble * 1000.0;
            Pulse.Enqueue(Make(PulseSchema.LoadKindColdStart, SceneManager.GetActiveScene().name, ms));
        }

        public static bool ColdStartReported { get { return _coldStartReported; } }

        public static void BeginSceneLoad(string sceneName)
        {
            _sceneStart = Time.realtimeSinceStartupAsDouble;
            _pendingScene = sceneName;
            _awaitFrame = false;
        }

        public static AsyncOperation LoadSceneAsync(string sceneName, LoadSceneMode mode = LoadSceneMode.Single)
        {
            BeginSceneLoad(sceneName);
            return SceneManager.LoadSceneAsync(sceneName, mode);
        }

        internal static void OnSceneLoaded(Scene scene, LoadSceneMode mode)
        {
            if (_pendingScene != null && (_pendingScene == scene.name || _pendingScene == scene.path)) _awaitFrame = true;
        }

        /// <summary>Called by the runtime once per frame (cheap boolean check).</summary>
        internal static void OnFrame()
        {
            if (!_awaitFrame) return;
            double ms = (Time.realtimeSinceStartupAsDouble - _sceneStart) * 1000.0;
            Pulse.Enqueue(Make(PulseSchema.LoadKindScene, _pendingScene, ms));
            _awaitFrame = false;
            _pendingScene = null;
            _sceneStart = -1;
        }

        static PulseEvent Make(string kind, string scene, double ms)
        {
            return new PulseEvent(PulseSchema.LoadEvent, PulseJson.ToUnixMs(System.DateTime.UtcNow))
                .AddText(PulseSchema.ParamTier, DeviceTier.Current)
                .AddText(PulseSchema.ParamKind, kind)
                .AddText(PulseSchema.ParamScene, scene)
                .AddDouble(PulseSchema.ParamDuration, ms);
        }
    }
}
