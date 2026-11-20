using System;
using System.Collections.Generic;
using UnityEngine;

namespace Studio.Pulse.Ads
{
    /// <summary>
    /// One call at startup turns on whatever Resources/studio_app.json configures: the Pulse
    /// analytics SDK, the ad mediation SDK (with its consent flow) and the MMP. An empty file,
    /// or none, starts nothing: no network call, no ad (docs/ADS_UNITY.md).
    /// <code>StudioApp.Start();   // e.g. in the first scene's Awake</code>
    /// </summary>
    public static class StudioApp
    {
        /// <summary>The Resources path of the config (Assets/Resources/studio_app.json).</summary>
        public const string ConfigResource = "studio_app";

        static readonly Dictionary<string, Func<IStudioAdsProvider>> Providers = new Dictionary<string, Func<IStudioAdsProvider>>();
        static readonly Dictionary<string, Func<IStudioMmp>> Mmps = new Dictionary<string, Func<IStudioMmp>>();
        static IStudioMmp _mmp;
        static bool _mmpStarted;

        public static bool Started { get; private set; }
        public static StudioAppConfig Config { get; private set; }

        /// <summary>"ios", "android", or "other" (no ads, no MMP).</summary>
        public static string Platform
        {
            get
            {
#if UNITY_IOS
                return "ios";
#elif UNITY_ANDROID
                return "android";
#else
                return "other";
#endif
            }
        }

        /// <summary>Called by the optional provider assemblies before the first scene loads.</summary>
        public static void RegisterAdsProvider(string name, Func<IStudioAdsProvider> factory)
        {
            if (!string.IsNullOrEmpty(name) && factory != null) Providers[name] = factory;
        }

        /// <summary>Called by the optional MMP assemblies before the first scene loads.</summary>
        public static void RegisterMmp(string name, Func<IStudioMmp> factory)
        {
            if (!string.IsNullOrEmpty(name) && factory != null) Mmps[name] = factory;
        }

        /// <summary>Reads Resources/studio_app.json; a missing or broken file gives an empty config.</summary>
        public static StudioAppConfig LoadConfig()
        {
            try
            {
                var asset = Resources.Load<TextAsset>(ConfigResource);
                if (asset == null) return new StudioAppConfig();
                var cfg = JsonUtility.FromJson<StudioAppConfig>(asset.text) ?? new StudioAppConfig();
                Resources.UnloadAsset(asset);
                return cfg;
            }
            catch (Exception ex)
            {
                Debug.LogWarning("[Studio.Ads] " + ConfigResource + ".json unreadable, everything stays off: " + ex.Message);
                return new StudioAppConfig();
            }
        }

        /// <summary>True once <see cref="Launch"/> ran.</summary>
        public static bool Launched { get; private set; }

        /// <summary>
        /// The one line a game needs, e.g. the first line of the first scene's Start (the app is
        /// active then, so the iOS tracking prompt can show). Once; later calls do nothing; never throws:
        /// <code>StudioApp.Launch(() => MyAnalytics.Bind(), () => MyRewards.Use(new MyAdsBridge()));</code>
        /// 1. <see cref="Start"/> (a failure is logged, the game goes on without analytics and ads);
        /// 2. <paramref name="started"/>, always: bind the game's analytics hooks there (Pulse.Core is
        ///    non-null when analytics is on; with a keyless config it is null and nothing is sent);
        /// 3. <paramref name="adsReady"/>, only when an ad provider is attached (StudioAds.Provider):
        ///    hand StudioAds to the game's own reward code there. With a keyless config it never runs,
        ///    so the game shows no ad button.
        /// </summary>
        public static void Launch(Action started = null, Action adsReady = null)
        {
            if (Launched) return;
            Launched = true;
            try { Start(); }
            catch (Exception ex) { Debug.LogWarning("[Studio.Ads] StudioApp.Start failed, analytics and ads stay off: " + ex.Message); }
            Call(started, "started");
            if (StudioAds.Provider != null) Call(adsReady, "adsReady");
        }

        static void Call(Action a, string what)
        {
            if (a == null) return;
            try { a(); }
            catch (Exception ex) { Debug.LogWarning("[Studio.Ads] StudioApp.Launch " + what + ": " + ex.GetType().Name + ": " + ex.Message); }
        }

        /// <summary>
        /// Starts what the config turns on (once; later calls do nothing). Pass a config to override
        /// the file (tests). Order: Pulse, then ads (consent flow), then the MMP after the flow.
        /// </summary>
        public static void Start(StudioAppConfig config = null)
        {
            if (Started) return;
            Started = true;
            Config = config ?? LoadConfig();
            string platform = Platform;
            bool storeBuild = !Debug.isDebugBuild;
            foreach (string problem in Config.Problems(platform, storeBuild)) Debug.LogWarning("[Studio.Ads] " + problem);

            if (Config.PulseConfigured && (!storeBuild || Config.pulse.collectorUrl.Trim().StartsWith("https://", StringComparison.OrdinalIgnoreCase)))
            {
                Pulse.Init(Config.pulse.writeKey.Trim(), Config.pulse.collectorUrl.Trim(),
                    new PulseOptions { ConsentRequired = Config.pulse.consentRequired });
            }

            _mmp = PickMmp(platform);
            string wanted = Config.AdsProvider(platform);
            Func<IStudioAdsProvider> factory = null;
            if (wanted != null && !Providers.TryGetValue(wanted, out factory))
                Debug.LogWarning("[Studio.Ads] " + wanted + " keys are set but its SDK is not in this build: ads stay off");
            if (factory == null && _mmp == null) return;

            StudioMainThread.Ensure();
            if (_mmp != null) PulseAdBridge.Impression += ForwardToMmp;
            if (factory != null)
            {
                StudioAds.Attach(factory(), Config, platform, StartMmp);
            }
            else
            {
                StartMmp();
            }
        }

        static IStudioMmp PickMmp(string platform)
        {
            foreach (var f in Mmps.Values)
            {
                IStudioMmp m = f();
                if (m != null && m.IsConfigured(Config, platform)) return m;
            }
            if (Config.MmpConfigured(platform) && Mmps.Count == 0)
                Debug.LogWarning("[Studio.Ads] the AppsFlyer dev key is set but the AppsFlyer SDK is not installed: MMP stays off");
            return null;
        }

        static void StartMmp()
        {
            if (_mmp == null || _mmpStarted) return;
            _mmpStarted = true;
            try { _mmp.Start(Config, Platform); }
            catch (Exception ex) { Debug.LogWarning("[Studio.Ads] " + _mmp.Name + " start failed: " + ex.Message); }
        }

        static void ForwardToMmp(PulseAdRevenue r)
        {
            StudioMainThread.Run(() =>
            {
                if (_mmp == null) return;
                try { _mmp.AdRevenue(r); }
                catch (Exception ex) { Debug.LogWarning("[Studio.Ads] " + _mmp.Name + " ad revenue: " + ex.Message); }
            });
        }
    }
}
