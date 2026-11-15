using System;
using UnityEngine;

namespace Studio.Pulse.Ads
{
    /// <summary>
    /// What a game calls to show ads, whatever the mediation SDK. Nothing is ready (and every
    /// Show call answers "no" at once) until StudioApp.Start found ad keys and an ad SDK.
    /// Callbacks always run on the main thread. Each step is logged as ad_event (request, fill,
    /// show, click, reward, fail); revenue arrives as ad_impression through the bridges.
    /// <code>
    /// if (StudioAds.RewardedReady) StudioAds.ShowRewarded("double_coins", ok => { if (ok) Give(); });
    /// StudioAds.ShowInterstitial("level_end", Continue);
    /// </code>
    /// </summary>
    public static class StudioAds
    {
        const string PacingKey = "studio_ads_pacing";
        static IStudioAdsProvider _provider;
        static PulseAdPacing _pacing;
        static bool _ready;
        static bool _busy;
        static readonly Events Sink = new Events();

        /// <summary>Ad availability may have changed (an ad loaded, or one was used). Main thread.</summary>
        public static event Action AvailabilityChanged;

        /// <summary>What SetGdprConsent set (null: not set). Read by the providers.</summary>
        public static bool? GdprConsentOverride { get; private set; }
        /// <summary>What SetDoNotSell set (null: not set). Read by the providers.</summary>
        public static bool? DoNotSellOverride { get; private set; }

        /// <summary>
        /// The GDPR consent the game collected itself (its own dialog), passed to the ad SDK before it
        /// starts; call before StudioApp.Start. Overrides privacy.gdprConsent. Not needed when Google
        /// UMP or another Additional Consent CMP ran: LevelPlay and MAX read that themselves.
        /// </summary>
        public static void SetGdprConsent(bool granted)
        {
            GdprConsentOverride = granted;
        }

        /// <summary>US state privacy laws: the user opted out of the sale of personal data. Call before StudioApp.Start.</summary>
        public static void SetDoNotSell(bool optedOut)
        {
            DoNotSellOverride = optedOut;
        }

        /// <summary>The provider's name (applovin_max), or null when ads are off.</summary>
        public static string Provider
        {
            get { return _provider != null ? _provider.Name : null; }
        }

        /// <summary>True once the SDK finished its consent flow and initialisation.</summary>
        public static bool Initialized
        {
            get { return _ready; }
        }

        public static bool RewardedReady
        {
            get { return _ready && !_busy && _provider.RewardedReady && _pacing.RewardedAllowed(Day()); }
        }

        public static bool InterstitialReady
        {
            get { return _ready && !_busy && _provider.InterstitialReady && _pacing.InterstitialAllowed(Unix(), Day()); }
        }

        /// <summary>Show a rewarded ad; done(true) only if the SDK granted the reward. Not ready: done(false) at once.</summary>
        public static void ShowRewarded(string placement, Action<bool> done)
        {
            if (!RewardedReady)
            {
                Finish(done, false);
                return;
            }
            _busy = true;
            _pacing.RewardedShown(Day());
            SavePacing();
            _provider.ShowRewarded(placement, ok => StudioMainThread.Run(() =>
            {
                _busy = false;
                Changed();
                Finish(done, ok);
            }));
        }

        /// <summary>Show an interstitial; done() after it closes, or at once when none can be shown.</summary>
        public static void ShowInterstitial(string placement, Action done)
        {
            if (!InterstitialReady)
            {
                if (done != null) done();
                return;
            }
            _busy = true;
            _pacing.InterstitialShown(Unix(), Day());
            SavePacing();
            _provider.ShowInterstitial(placement, () => StudioMainThread.Run(() =>
            {
                _busy = false;
                Changed();
                if (done != null) done();
            }));
        }

        /// <summary>Shows or hides the banner (if the config has a banner ad unit).</summary>
        public static void SetBannerVisible(bool visible)
        {
            if (_provider != null && _provider.HasBanner) _provider.SetBannerVisible(visible);
        }

        /// <summary>True for users who must be offered a "Privacy settings" button (GDPR regions).</summary>
        public static bool PrivacyOptionsRequired
        {
            get { return _ready && _provider.PrivacyOptionsRequired; }
        }

        /// <summary>Shows the consent form again (call from the game's settings button).</summary>
        public static void ShowPrivacyOptions(Action done)
        {
            if (!PrivacyOptionsRequired)
            {
                if (done != null) done();
                return;
            }
            _provider.ShowPrivacyOptions(() => StudioMainThread.Run(done));
        }

        internal static void Attach(IStudioAdsProvider provider, StudioAppConfig config, string platform, Action consentDone)
        {
            if (provider == null || _provider != null) return;
            _provider = provider;
            _pacing = new PulseAdPacing(config.pacing, LoadPacing(config.pacing));
            try
            {
                provider.Initialize(config, platform, Sink, () => StudioMainThread.Run(() =>
                {
                    _ready = true;
                    Changed();
                    if (consentDone != null) consentDone();
                }));
            }
            catch (Exception ex)
            {
                Debug.LogWarning("[Studio.Ads] " + provider.Name + " failed to start: " + ex.Message);
                if (consentDone != null) consentDone();
            }
        }

        static void Finish(Action<bool> done, bool ok)
        {
            if (done != null) done(ok);
        }

        static void Changed()
        {
            Action a = AvailabilityChanged;
            if (a == null) return;
            try { a(); }
            catch (Exception ex) { Debug.LogException(ex); }
        }

        static long Unix()
        {
            return DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        }

        static long Day()
        {
            return (long)(DateTime.Now.Date - new DateTime(1970, 1, 1)).TotalDays;
        }

        static bool PacingUsed(StudioAppConfig.PacingSection p)
        {
            return p != null && (p.interstitialsPerDay > 0 || p.rewardedPerDay > 0 || p.interstitialMinSeconds > 0);
        }

        static PulseAdPacing.State LoadPacing(StudioAppConfig.PacingSection p)
        {
            if (!PacingUsed(p)) return default(PulseAdPacing.State);
            try { return JsonUtility.FromJson<PulseAdPacing.State>(PlayerPrefs.GetString(PacingKey, "{}")); }
            catch (Exception) { return default(PulseAdPacing.State); }
        }

        static void SavePacing()
        {
            if (!PacingUsed(StudioApp.Config != null ? StudioApp.Config.pacing : null)) return;
            PlayerPrefs.SetString(PacingKey, JsonUtility.ToJson(_pacing.Now));
            PlayerPrefs.Save();
        }

        /// <summary>Provider events -> ad_event (main thread).</summary>
        sealed class Events : IStudioAdsEvents
        {
            public void LoadStarted(string format) { Pulse.AdEvent(PulseContract.AdAction.Request, format); }
            public void Loaded(string format) { Pulse.AdEvent(PulseContract.AdAction.Fill, format); StudioMainThread.Run(Changed); }

            public void LoadFailed(string format, string errorCode, int attempt)
            {
                if (attempt <= 1) Pulse.AdEvent(PulseContract.AdAction.Fail, format, "load", errorCode);
            }

            public void Shown(string format, string placement) { Pulse.AdEvent(PulseContract.AdAction.Show, format, placement); }
            public void ShowFailed(string format, string placement, string errorCode) { Pulse.AdEvent(PulseContract.AdAction.Fail, format, placement, errorCode); }
            public void Clicked(string format, string placement) { Pulse.AdEvent(PulseContract.AdAction.Click, format, placement); }
            public void Rewarded(string placement) { Pulse.AdEvent(PulseContract.AdAction.Reward, "rewarded", placement); }
            public void Closed(string format, string placement) { StudioMainThread.Run(Changed); }
        }
    }
}
