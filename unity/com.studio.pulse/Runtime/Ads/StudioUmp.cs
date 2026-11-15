using System;
#if UNITY_IOS && !UNITY_EDITOR && STUDIO_PULSE_UMP
using System.Runtime.InteropServices;
#endif
using UnityEngine;

namespace Studio.Pulse.Ads
{
    /// <summary>
    /// Google User Messaging Platform: the GDPR / UK consent form, asked before the ad SDK starts.
    /// LevelPlay (SDK 7.7.0+) reads the consent UMP stores (IAB TCF string + Google Additional Consent)
    /// by itself, so nothing is passed on. Compiled only with the STUDIO_PULSE_UMP define, with the
    /// native UMP SDK in the build (iOS pod GoogleUserMessagingPlatform 3.x, Android
    /// com.google.android.ump:user-messaging-platform 4.x: Samples~/AdsQuickStart/StudioPulseUmpDependencies.xml).
    /// Without the define, on other platforms, or with an empty AdMob app id, Gather answers at once.
    /// UMP itself decides where the form is required (EEA, UK, Switzerland) and shows it only there.
    /// </summary>
    public static class StudioUmp
    {
        /// <summary>The longest wait for UMP's consent-information request before ads start anyway.</summary>
        public const double RequestTimeoutSeconds = 12;

        /// <summary>True when this build contains the UMP bridge.</summary>
        public static bool Available
        {
            get
            {
#if (UNITY_IOS || UNITY_ANDROID) && !UNITY_EDITOR && STUDIO_PULSE_UMP
                return true;
#else
                return false;
#endif
            }
        }

        static Action<bool> _pending;
        static bool _ran;

        /// <summary>Last answer of canRequestAds (true when UMP did not run).</summary>
        public static bool CanRequestAds { get; private set; } = true;

        static void Finish(bool canRequestAds, string error)
        {
            Action<bool> d = _pending;
            _pending = null;
            _formShowing = false;
            if (d == null) return;
            CanRequestAds = canRequestAds;
            if (!string.IsNullOrEmpty(error)) Debug.LogWarning("[Studio.Ads] consent (UMP): " + error);
            Debug.Log("[Studio.Ads] consent (UMP) done, canRequestAds=" + canRequestAds);
            d(canRequestAds);
        }

        /// <summary>
        /// Updates the consent information and shows the form if UMP says it is required; done(canRequestAds)
        /// on the main thread. Debug geography and test devices apply in development builds only.
        /// </summary>
        public static void Gather(StudioAppConfig config, string platform, Action<bool> done)
        {
            string appId = config != null ? config.UmpAppId(platform) : "";
            if (!Available || appId.Length == 0 || _ran)
            {
                if (done != null) done(CanRequestAds);
                return;
            }
            _ran = true;
            _pending = ok => { if (done != null) done(ok); };
            bool dev = Debug.isDebugBuild;
            int geography = dev ? StudioAppConfig.UmpDebugGeography(config.ump.debugGeography) : 0;
            string tests = dev && config.ump.testDeviceIds != null ? string.Join(",", config.ump.testDeviceIds) : "";
            // A request that never answers (no network) must not keep the ads off for the whole session.
            StudioMainThread.Delay(RequestTimeoutSeconds, () => { if (_pending != null && !_formShowing) Finish(CanRequestAdsNow(), "timed out"); });
            try
            {
                GatherNative(appId, geography, tests);
            }
            catch (Exception ex)
            {
                Finish(true, ex.Message);
            }
        }

        static bool _formShowing;

        /// <summary>True for users who must be offered a "Privacy settings" button (UMP decides; EEA / UK).</summary>
        public static bool PrivacyOptionsRequired
        {
            get
            {
                if (!Available || !_ran) return false;
                try { return PrivacyRequiredNative(); }
                catch (Exception) { return false; }
            }
        }

        /// <summary>Shows UMP's privacy options form again (the game's settings button); done() on the main thread.</summary>
        public static void ShowPrivacyOptions(Action done)
        {
            if (!PrivacyOptionsRequired)
            {
                if (done != null) done();
                return;
            }
            try { ShowPrivacyNative(() => StudioMainThread.Run(done)); }
            catch (Exception ex)
            {
                Debug.LogWarning("[Studio.Ads] UMP privacy options: " + ex.Message);
                if (done != null) done();
            }
        }

        static bool CanRequestAdsNow()
        {
            try { return CanRequestNative(); }
            catch (Exception) { return true; }
        }

#if UNITY_IOS && !UNITY_EDITOR && STUDIO_PULSE_UMP
        delegate void Callback(int canRequestAds, string error);
        delegate void Phase();

        [DllImport("__Internal")] static extern void StudioPulseUmpGather(int geography, string testDeviceIds, Phase updated, Callback callback);
        [DllImport("__Internal")] static extern int StudioPulseUmpCanRequestAds();
        [DllImport("__Internal")] static extern int StudioPulseUmpPrivacyOptionsRequired();
        [DllImport("__Internal")] static extern void StudioPulseUmpShowPrivacyOptions(Callback callback);

        static Action _privacyDone;

        [AOT.MonoPInvokeCallback(typeof(Phase))]
        static void OnUpdated()
        {
            // The form may be on screen now: the request timeout no longer applies.
            StudioMainThread.Run(() => _formShowing = true);
        }

        [AOT.MonoPInvokeCallback(typeof(Callback))]
        static void OnGathered(int canRequestAds, string error)
        {
            StudioMainThread.Run(() => Finish(canRequestAds != 0, error));
        }

        [AOT.MonoPInvokeCallback(typeof(Callback))]
        static void OnPrivacyClosed(int canRequestAds, string error)
        {
            Action d = _privacyDone;
            _privacyDone = null;
            CanRequestAds = canRequestAds != 0;
            if (d != null) d();
        }

        static void GatherNative(string appId, int geography, string tests)
        {
            // iOS takes the app id from Info.plist (GADApplicationIdentifier), written by the build step.
            StudioPulseUmpGather(geography, tests, OnUpdated, OnGathered);
        }

        static bool CanRequestNative() { return StudioPulseUmpCanRequestAds() != 0; }
        static bool PrivacyRequiredNative() { return StudioPulseUmpPrivacyOptionsRequired() != 0; }

        static void ShowPrivacyNative(Action done)
        {
            _privacyDone = done;
            StudioPulseUmpShowPrivacyOptions(OnPrivacyClosed);
        }
#elif UNITY_ANDROID && !UNITY_EDITOR && STUDIO_PULSE_UMP
        const string Ump = "com.google.android.ump.";

        static AndroidJavaObject Activity()
        {
            using (var player = new AndroidJavaClass("com.unity3d.player.UnityPlayer"))
                return player.GetStatic<AndroidJavaObject>("currentActivity");
        }

        static AndroidJavaObject Info(AndroidJavaObject activity)
        {
            using (var ump = new AndroidJavaClass(Ump + "UserMessagingPlatform"))
                return ump.CallStatic<AndroidJavaObject>("getConsentInformation", activity);
        }

        sealed class Listener : AndroidJavaProxy
        {
            readonly Action<AndroidJavaObject> _call;

            public Listener(string iface, Action<AndroidJavaObject> call) : base(Ump + iface) { _call = call; }

            // OnConsentInfoUpdateSuccessListener
            void onConsentInfoUpdateSuccess() { _call(null); }

            // OnConsentInfoUpdateFailureListener / OnConsentFormDismissedListener (FormError may be null)
            void onConsentInfoUpdateFailure(AndroidJavaObject error) { _call(error ?? new AndroidJavaObject(Ump + "FormError", 0, "unknown")); }
            void onConsentFormDismissed(AndroidJavaObject error) { _call(error); }
        }

        static string Message(AndroidJavaObject error)
        {
            return error == null ? null : error.Call<string>("getMessage");
        }

        static void GatherNative(string appId, int geography, string tests)
        {
            AndroidJavaObject activity = Activity();
            var builder = new AndroidJavaObject(Ump + "ConsentRequestParameters$Builder");
            builder.Call<AndroidJavaObject>("setTagForUnderAgeOfConsent", false);
            builder.Call<AndroidJavaObject>("setAdMobAppId", appId);
            if (geography > 0)
            {
                var debug = new AndroidJavaObject(Ump + "ConsentDebugSettings$Builder", activity);
                debug.Call<AndroidJavaObject>("setDebugGeography", geography);
                foreach (string id in tests.Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries))
                    debug.Call<AndroidJavaObject>("addTestDeviceHashedId", id.Trim());
                builder.Call<AndroidJavaObject>("setConsentDebugSettings", debug.Call<AndroidJavaObject>("build"));
            }
            AndroidJavaObject parameters = builder.Call<AndroidJavaObject>("build");
            AndroidJavaObject info = Info(activity);
            activity.Call("runOnUiThread", new AndroidJavaRunnable(() =>
            {
                info.Call("requestConsentInfoUpdate", activity, parameters,
                    new Listener("ConsentInformation$OnConsentInfoUpdateSuccessListener", _ =>
                    {
                        _formShowing = true;
                        activity.Call("runOnUiThread", new AndroidJavaRunnable(() =>
                        {
                            using (var ump = new AndroidJavaClass(Ump + "UserMessagingPlatform"))
                            {
                                ump.CallStatic("loadAndShowConsentFormIfRequired", activity,
                                    new Listener("ConsentForm$OnConsentFormDismissedListener", err =>
                                    {
                                        string m = Message(err);
                                        bool can = info.Call<bool>("canRequestAds");
                                        StudioMainThread.Post(() => { _formShowing = false; Finish(can, m); });
                                    }));
                            }
                        }));
                    }),
                    new Listener("ConsentInformation$OnConsentInfoUpdateFailureListener", err =>
                    {
                        string m = Message(err);
                        bool can = info.Call<bool>("canRequestAds");
                        StudioMainThread.Post(() => Finish(can, m));
                    }));
            }));
        }

        static bool CanRequestNative() { return Info(Activity()).Call<bool>("canRequestAds"); }

        static bool PrivacyRequiredNative()
        {
            using (AndroidJavaObject status = Info(Activity()).Call<AndroidJavaObject>("getPrivacyOptionsRequirementStatus"))
                return status != null && status.Call<string>("name") == "REQUIRED";
        }

        static void ShowPrivacyNative(Action done)
        {
            AndroidJavaObject activity = Activity();
            activity.Call("runOnUiThread", new AndroidJavaRunnable(() =>
            {
                using (var ump = new AndroidJavaClass(Ump + "UserMessagingPlatform"))
                    ump.CallStatic("showPrivacyOptionsForm", activity,
                        new Listener("ConsentForm$OnConsentFormDismissedListener", _ => done()));
            }));
        }
#else
        static void GatherNative(string appId, int geography, string tests) { Finish(true, null); }
        static bool CanRequestNative() { return true; }
        static bool PrivacyRequiredNative() { return false; }
        static void ShowPrivacyNative(Action done) { done(); }
#endif
    }
}
