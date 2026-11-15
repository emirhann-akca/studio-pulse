// Compiled only when AppLovin MAX is in the project (Studio.Pulse.Ads.Max.asmdef). Follows the
// AppLovin MAX Unity guides (checked against plugin 8.6.6): initialise once at launch (the Terms
// and Privacy Policy flow, ATT and Google UMP, runs inside InitializeSdk), load after the init
// callback, retry failed loads after 2^min(6, n) seconds, reload after an ad is hidden or failed
// to display, reward only in OnAdReceivedRewardEvent. The SDK key is not set here: MAX reads it
// from AppLovinSettings, which Editor/Ads/Max syncs from studio_app.json before every build.
using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Scripting;

[assembly: AlwaysLinkAssembly]

namespace Studio.Pulse.Ads
{
    [Preserve]
    sealed class MaxAdsProvider : IStudioAdsProvider
    {
        const string Rewarded = "rewarded", Interstitial = "interstitial", Banner = "banner";
        /// <summary>How long to wait after a rewarded ad closes for a late reward callback.</summary>
        const double RewardGraceSeconds = 1.0;

        [Preserve]
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterAssembliesLoaded)]
        static void Register()
        {
            StudioApp.RegisterAdsProvider(PulseAdRevenueMap.PlatformMax, () => new MaxAdsProvider());
        }

        StudioAppConfig.AdUnitIds _ids;
        IStudioAdsEvents _ev;
        Action _ready;
        bool _initialized;
        MaxSdkBase.AdViewPosition _bannerPosition = MaxSdkBase.AdViewPosition.BottomCenter;
        readonly PulseAdRetry _rewardedRetry = new PulseAdRetry();
        readonly PulseAdRetry _interRetry = new PulseAdRetry();

        Action<bool> _rewardDone;
        string _rewardPlacement;
        bool _rewardEarned;
        Action _interDone;
        string _interPlacement;
        bool _bannerCreated, _bannerWanted, _bannerLoggedFill, _bannerLoggedFail;

        public string Name
        {
            get { return PulseAdRevenueMap.PlatformMax; }
        }

        public void Initialize(StudioAppConfig config, string platform, IStudioAdsEvents events, Action ready)
        {
            if (config.privacy != null && config.privacy.childDirected)
            {
                // AppLovin's terms: the SDK must not be initialised for children at all.
                Debug.LogWarning("[Studio.Ads] privacy.childDirected: AppLovin MAX is not started (AppLovin does not allow child users)");
                if (ready != null) ready();
                return;
            }
            _ids = config.UnitsFor(StudioAppConfig.Max, platform) ?? new StudioAppConfig.AdUnitIds();
            PulseAdBridge.Register(PulseAdRevenueMap.PlatformMax);
            // Privacy flags before InitializeSdk (MAX docs); the consent flow sets them itself when on.
            bool? gdpr = StudioAds.GdprConsentOverride ?? config.GdprConsent;
            if (gdpr.HasValue) MaxSdk.SetHasUserConsent(gdpr.Value);
            if (StudioAds.DoNotSellOverride.HasValue) MaxSdk.SetDoNotSell(StudioAds.DoNotSellOverride.Value);
            _ev = events;
            _ready = ready;
            if (config.max.bannerPosition == "top") _bannerPosition = MaxSdkBase.AdViewPosition.TopCenter;
            // Every MAX callback on the main thread (revenue too; the Pulse bridge is thread-safe either way).
            MaxSdkBase.InvokeEventsOnUnityMainThread = true;
            Subscribe();
            if (config.max.verboseLogging) MaxSdk.SetVerboseLogging(true);
            MaxSdkCallbacks.OnSdkInitializedEvent += OnInitialized;
            var units = new List<string>(3);
            foreach (string id in new[] { _ids.rewarded, _ids.interstitial, _ids.banner })
            {
                if (!StudioAppConfig.Blank(id)) units.Add(id.Trim());
            }
            MaxSdk.InitializeSdk(units.ToArray());
        }

        void OnInitialized(MaxSdkBase.SdkConfiguration sdk)
        {
            if (_initialized) return;
            _initialized = true;
            if (sdk != null && !sdk.IsSuccessfullyInitialized)
                Debug.LogWarning("[Studio.Ads] AppLovin MAX did not initialise successfully (check the SDK key); ads will retry loading");
            LoadRewarded();
            LoadInterstitial();
            if (_bannerWanted) SetBannerVisible(true);
            Action r = _ready;
            _ready = null;
            if (r != null) r();
        }

        void Subscribe()
        {
            MaxSdkCallbacks.Rewarded.OnAdLoadedEvent += (id, info) => { if (Is(id, _ids.rewarded)) { _rewardedRetry.Loaded(); _ev.Loaded(Rewarded); } };
            MaxSdkCallbacks.Rewarded.OnAdLoadFailedEvent += (id, err) => { if (Is(id, _ids.rewarded)) Retry(Rewarded, _rewardedRetry, err, LoadRewarded); };
            MaxSdkCallbacks.Rewarded.OnAdDisplayedEvent += (id, info) => { if (Is(id, _ids.rewarded)) _ev.Shown(Rewarded, _rewardPlacement); };
            MaxSdkCallbacks.Rewarded.OnAdClickedEvent += (id, info) => { if (Is(id, _ids.rewarded)) _ev.Clicked(Rewarded, _rewardPlacement); };
            MaxSdkCallbacks.Rewarded.OnAdReceivedRewardEvent += (id, reward, info) =>
            {
                if (!Is(id, _ids.rewarded)) return;
                _rewardEarned = true;
                _ev.Rewarded(_rewardPlacement);
            };
            MaxSdkCallbacks.Rewarded.OnAdDisplayFailedEvent += (id, err, info) =>
            {
                if (!Is(id, _ids.rewarded)) return;
                _ev.ShowFailed(Rewarded, _rewardPlacement, Code(err));
                FinishRewarded(false);
                LoadRewarded();
            };
            MaxSdkCallbacks.Rewarded.OnAdHiddenEvent += (id, info) =>
            {
                if (!Is(id, _ids.rewarded)) return;
                _ev.Closed(Rewarded, _rewardPlacement);
                LoadRewarded();
                // Some networks report the reward just after the ad closes: wait a moment for it.
                if (_rewardEarned) FinishRewarded(true);
                else StudioMainThread.Delay(RewardGraceSeconds, () => FinishRewarded(_rewardEarned));
            };

            MaxSdkCallbacks.Interstitial.OnAdLoadedEvent += (id, info) => { if (Is(id, _ids.interstitial)) { _interRetry.Loaded(); _ev.Loaded(Interstitial); } };
            MaxSdkCallbacks.Interstitial.OnAdLoadFailedEvent += (id, err) => { if (Is(id, _ids.interstitial)) Retry(Interstitial, _interRetry, err, LoadInterstitial); };
            MaxSdkCallbacks.Interstitial.OnAdDisplayedEvent += (id, info) => { if (Is(id, _ids.interstitial)) _ev.Shown(Interstitial, _interPlacement); };
            MaxSdkCallbacks.Interstitial.OnAdClickedEvent += (id, info) => { if (Is(id, _ids.interstitial)) _ev.Clicked(Interstitial, _interPlacement); };
            MaxSdkCallbacks.Interstitial.OnAdDisplayFailedEvent += (id, err, info) =>
            {
                if (!Is(id, _ids.interstitial)) return;
                _ev.ShowFailed(Interstitial, _interPlacement, Code(err));
                FinishInterstitial();
                LoadInterstitial();
            };
            MaxSdkCallbacks.Interstitial.OnAdHiddenEvent += (id, info) =>
            {
                if (!Is(id, _ids.interstitial)) return;
                _ev.Closed(Interstitial, _interPlacement);
                FinishInterstitial();
                LoadInterstitial();
            };

            MaxSdkCallbacks.Banner.OnAdLoadedEvent += (id, info) =>
            {
                if (!Is(id, _ids.banner) || _bannerLoggedFill) return;
                _bannerLoggedFill = true;
                _ev.Loaded(Banner);
            };
            MaxSdkCallbacks.Banner.OnAdLoadFailedEvent += (id, err) =>
            {
                // Banners refresh and retry by themselves; log the first failure only.
                if (!Is(id, _ids.banner) || _bannerLoggedFail) return;
                _bannerLoggedFail = true;
                _ev.LoadFailed(Banner, Code(err), 1);
            };
            MaxSdkCallbacks.Banner.OnAdClickedEvent += (id, info) => { if (Is(id, _ids.banner)) _ev.Clicked(Banner, null); };
        }

        static bool Is(string id, string configured)
        {
            return !StudioAppConfig.Blank(configured) && string.Equals(id, configured.Trim(), StringComparison.Ordinal);
        }

        static string Code(MaxSdkBase.ErrorInfo err)
        {
            return err == null ? null : ((int)err.Code).ToString(System.Globalization.CultureInfo.InvariantCulture);
        }

        void Retry(string format, PulseAdRetry retry, MaxSdkBase.ErrorInfo err, Action load)
        {
            double delay = retry.Failed();
            _ev.LoadFailed(format, Code(err), retry.Attempt);
            StudioMainThread.Delay(delay, load);
        }

        void LoadRewarded()
        {
            if (!_initialized || StudioAppConfig.Blank(_ids.rewarded)) return;
            if (_rewardedRetry.Attempt == 0) _ev.LoadStarted(Rewarded);
            MaxSdk.LoadRewardedAd(_ids.rewarded.Trim());
        }

        void LoadInterstitial()
        {
            if (!_initialized || StudioAppConfig.Blank(_ids.interstitial)) return;
            if (_interRetry.Attempt == 0) _ev.LoadStarted(Interstitial);
            MaxSdk.LoadInterstitial(_ids.interstitial.Trim());
        }

        public bool RewardedReady
        {
            get { return _initialized && _rewardDone == null && !StudioAppConfig.Blank(_ids.rewarded) && MaxSdk.IsRewardedAdReady(_ids.rewarded.Trim()); }
        }

        public bool InterstitialReady
        {
            get { return _initialized && _interDone == null && !StudioAppConfig.Blank(_ids.interstitial) && MaxSdk.IsInterstitialReady(_ids.interstitial.Trim()); }
        }

        public bool HasBanner
        {
            get { return _ids != null && !StudioAppConfig.Blank(_ids.banner); }
        }

        public void ShowRewarded(string placement, Action<bool> done)
        {
            if (!RewardedReady)
            {
                if (done != null) done(false);
                return;
            }
            _rewardDone = done ?? (_ => { });
            _rewardPlacement = placement;
            _rewardEarned = false;
            MaxSdk.ShowRewardedAd(_ids.rewarded.Trim(), placement);
        }

        void FinishRewarded(bool ok)
        {
            Action<bool> d = _rewardDone;
            if (d == null) return;
            _rewardDone = null;
            _rewardEarned = false;
            d(ok);
        }

        public void ShowInterstitial(string placement, Action done)
        {
            if (!InterstitialReady)
            {
                if (done != null) done();
                return;
            }
            _interDone = done ?? (() => { });
            _interPlacement = placement;
            MaxSdk.ShowInterstitial(_ids.interstitial.Trim(), placement);
        }

        void FinishInterstitial()
        {
            Action d = _interDone;
            if (d == null) return;
            _interDone = null;
            d();
        }

        public void SetBannerVisible(bool visible)
        {
            _bannerWanted = visible;
            if (!_initialized || !HasBanner) return;
            string id = _ids.banner.Trim();
            if (visible && !_bannerCreated)
            {
                _bannerCreated = true;
                MaxSdk.CreateBanner(id, new MaxSdkBase.AdViewConfiguration(_bannerPosition));
            }
            if (!_bannerCreated) return;
            if (visible) MaxSdk.ShowBanner(id);
            else MaxSdk.HideBanner(id);
        }

        public bool PrivacyOptionsRequired
        {
            get
            {
                if (!_initialized) return false;
                MaxSdkBase.SdkConfiguration c = MaxSdk.GetSdkConfiguration();
                return c != null && c.ConsentFlowUserGeography == MaxSdkBase.ConsentFlowUserGeography.Gdpr
                    && MaxSdk.CmpService != null && MaxSdk.CmpService.HasSupportedCmp;
            }
        }

        public void ShowPrivacyOptions(Action done)
        {
            if (!PrivacyOptionsRequired)
            {
                if (done != null) done();
                return;
            }
            MaxSdk.CmpService.ShowCmpForExistingUser(error => { if (done != null) done(); });
        }
    }
}
