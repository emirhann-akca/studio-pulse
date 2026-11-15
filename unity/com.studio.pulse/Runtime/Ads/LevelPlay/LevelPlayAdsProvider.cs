// Compiled only when Unity LevelPlay 9.5+ is in the project (com.unity.services.levelplay,
// Studio.Pulse.Ads.LevelPlay.asmdef). Follows Unity's LevelPlay guides, checked against package
// 9.6.0: privacy flags (LevelPlayPrivacySettings.SetCOPPA / SetGDPRConsent / SetCCPA) and the
// iOS ATT prompt BEFORE LevelPlay.Init (and Google UMP's consent form before both, StudioUmp);
// ad objects created after OnInitSuccess
// (LevelPlayRewardedAd, LevelPlayInterstitialAd, LevelPlayBannerAd); reward only in OnAdRewarded
// (it may arrive after OnAdClosed); reload after an ad closes or fails to show; failed loads
// retried with exponential backoff (2^min(6, n) s); impression-level revenue from each ad's
// OnAdImpressionDataReady (9.5+; raised on a background thread). GDPR consent collected by Google
// UMP or an Additional Consent CMP is read by LevelPlay itself (SDK 7.7.0+).
using System;
using Unity.Services.LevelPlay;
using UnityEngine;
using UnityEngine.Scripting;

[assembly: AlwaysLinkAssembly]

namespace Studio.Pulse.Ads
{
    [Preserve]
    sealed class LevelPlayAdsProvider : IStudioAdsProvider
    {
        const string Rewarded = "rewarded", Interstitial = "interstitial", Banner = "banner";
        /// <summary>How long to wait after a rewarded ad closes for a late OnAdRewarded.</summary>
        const double RewardGraceSeconds = 2.0;

        [Preserve]
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterAssembliesLoaded)]
        static void Register()
        {
            StudioApp.RegisterAdsProvider(StudioAppConfig.LevelPlay, () => new LevelPlayAdsProvider());
        }

        StudioAppConfig _cfg;
        StudioAppConfig.AdUnitIds _ids;
        string _appKey;
        IStudioAdsEvents _ev;
        Action _ready;
        bool _initialized;
        LevelPlayRewardedAd _rewarded;
        LevelPlayInterstitialAd _interstitial;
        LevelPlayBannerAd _banner;
        bool _bannerWanted, _bannerLoggedFill, _bannerLoggedFail;
        readonly PulseAdRetry _rewardedRetry = new PulseAdRetry();
        readonly PulseAdRetry _interRetry = new PulseAdRetry();

        Action<bool> _rewardDone;
        string _rewardPlacement;
        bool _rewardEarned;
        Action _interDone;
        string _interPlacement;

        public string Name
        {
            get { return PulseAdRevenueMap.PlatformLevelPlay; }
        }

        public void Initialize(StudioAppConfig config, string platform, IStudioAdsEvents events, Action ready)
        {
            _cfg = config;
            _ids = config.UnitsFor(StudioAppConfig.LevelPlay, platform) ?? new StudioAppConfig.AdUnitIds();
            _appKey = config.LevelPlayAppKey(platform);
            _ev = events;
            _ready = ready;
            PulseAdBridge.Register(PulseAdRevenueMap.PlatformLevelPlay);

            // Order (Google / LevelPlay guidance): GDPR / UK consent form (UMP), then the iOS tracking prompt,
            // then the privacy flags and LevelPlay.Init. LevelPlay reads UMP's stored consent itself.
            StudioUmp.Gather(config, platform, _ =>
            {
                bool att = platform == "ios" && config.AttWanted(StudioAppConfig.LevelPlay) && config.AttText("en") != null;
                if (att && StudioAtt.Available) StudioAtt.Request(__ => StartSdk());
                else
                {
                    if (att) StudioAtt.WarnIfMissing("LevelPlay starts without the tracking prompt");
                    StartSdk();
                }
            });
        }

        void StartSdk()
        {
            // Privacy flags before Init (LevelPlay regulation settings).
            if (_cfg.privacy != null && _cfg.privacy.childDirected) LevelPlayPrivacySettings.SetCOPPA(true);
            bool? gdpr = StudioAds.GdprConsentOverride ?? _cfg.GdprConsent;
            if (gdpr.HasValue) LevelPlayPrivacySettings.SetGDPRConsent(gdpr.Value);
            if (StudioAds.DoNotSellOverride.HasValue) LevelPlayPrivacySettings.SetCCPA(StudioAds.DoNotSellOverride.Value);
            LevelPlay.OnInitSuccess += OnInitSuccess;
            LevelPlay.OnInitFailed += OnInitFailed;
            if (_cfg.levelplay.testSuite && Debug.isDebugBuild) LevelPlay.SetMetaData("is_test_suite", "enable");
            LevelPlay.Init(_appKey);
        }

        void OnInitFailed(LevelPlayInitError error)
        {
            StudioMainThread.Run(() =>
            {
                Debug.LogWarning("[Studio.Ads] LevelPlay init failed: " + (error != null ? error.ErrorMessage : "unknown") + "; no ads this session");
                Ready();
            });
        }

        void OnInitSuccess(LevelPlayConfiguration configuration)
        {
            StudioMainThread.Run(() =>
            {
                if (_initialized) return;
                _initialized = true;
                CreateRewarded();
                CreateInterstitial();
                if (_bannerWanted) SetBannerVisible(true);
                if (_cfg.levelplay.testSuite && Debug.isDebugBuild) LevelPlay.LaunchTestSuite();
                Ready();
            });
        }

        void Ready()
        {
            Action r = _ready;
            _ready = null;
            if (r != null) r();
        }

        // ------------------------------------------------------------------ rewarded

        void CreateRewarded()
        {
            if (StudioAppConfig.Blank(_ids.rewarded)) return;
            _rewarded = new LevelPlayRewardedAd(_ids.rewarded.Trim());
            _rewarded.OnAdLoaded += info => Main(() => { _rewardedRetry.Loaded(); _ev.Loaded(Rewarded); });
            _rewarded.OnAdLoadFailed += err => Main(() => Retry(Rewarded, _rewardedRetry, err, LoadRewarded));
            _rewarded.OnAdDisplayed += info => Main(() => _ev.Shown(Rewarded, _rewardPlacement));
            _rewarded.OnAdClicked += info => Main(() => _ev.Clicked(Rewarded, _rewardPlacement));
            _rewarded.OnAdRewarded += (info, reward) => Main(() =>
            {
                _rewardEarned = true;
                _ev.Rewarded(_rewardPlacement);
                if (_rewardDone != null && _rewardClosed) FinishRewarded(true);
            });
            _rewarded.OnAdDisplayFailed += (info, err) => Main(() =>
            {
                _ev.ShowFailed(Rewarded, _rewardPlacement, Code(err));
                FinishRewarded(false);
                LoadRewarded();
            });
            _rewarded.OnAdClosed += info => Main(() =>
            {
                _ev.Closed(Rewarded, _rewardPlacement);
                _rewardClosed = true;
                LoadRewarded();
                // OnAdRewarded may come after OnAdClosed (LevelPlay docs): wait a moment for it.
                if (_rewardEarned) FinishRewarded(true);
                else StudioMainThread.Delay(RewardGraceSeconds, () => FinishRewarded(_rewardEarned));
            });
            _rewarded.OnAdImpressionDataReady += Revenue;
            LoadRewarded();
        }

        bool _rewardClosed;

        void LoadRewarded()
        {
            if (_rewarded == null) return;
            if (_rewardedRetry.Attempt == 0) _ev.LoadStarted(Rewarded);
            _rewarded.LoadAd();
        }

        public bool RewardedReady
        {
            get { return _initialized && _rewarded != null && _rewardDone == null && _rewarded.IsAdReady(); }
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
            _rewardClosed = false;
            _rewarded.ShowAd(placement);
        }

        void FinishRewarded(bool ok)
        {
            Action<bool> d = _rewardDone;
            if (d == null) return;
            _rewardDone = null;
            _rewardEarned = false;
            _rewardClosed = false;
            d(ok);
        }

        // ------------------------------------------------------------------ interstitial

        void CreateInterstitial()
        {
            if (StudioAppConfig.Blank(_ids.interstitial)) return;
            _interstitial = new LevelPlayInterstitialAd(_ids.interstitial.Trim());
            _interstitial.OnAdLoaded += info => Main(() => { _interRetry.Loaded(); _ev.Loaded(Interstitial); });
            _interstitial.OnAdLoadFailed += err => Main(() => Retry(Interstitial, _interRetry, err, LoadInterstitial));
            _interstitial.OnAdDisplayed += info => Main(() => _ev.Shown(Interstitial, _interPlacement));
            _interstitial.OnAdClicked += info => Main(() => _ev.Clicked(Interstitial, _interPlacement));
            _interstitial.OnAdDisplayFailed += (info, err) => Main(() =>
            {
                _ev.ShowFailed(Interstitial, _interPlacement, Code(err));
                FinishInterstitial();
                LoadInterstitial();
            });
            _interstitial.OnAdClosed += info => Main(() =>
            {
                _ev.Closed(Interstitial, _interPlacement);
                FinishInterstitial();
                LoadInterstitial();
            });
            _interstitial.OnAdImpressionDataReady += Revenue;
            LoadInterstitial();
        }

        void LoadInterstitial()
        {
            if (_interstitial == null) return;
            if (_interRetry.Attempt == 0) _ev.LoadStarted(Interstitial);
            _interstitial.LoadAd();
        }

        public bool InterstitialReady
        {
            get { return _initialized && _interstitial != null && _interDone == null && _interstitial.IsAdReady(); }
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
            _interstitial.ShowAd(placement);
        }

        void FinishInterstitial()
        {
            Action d = _interDone;
            if (d == null) return;
            _interDone = null;
            d();
        }

        // ------------------------------------------------------------------ banner

        public bool HasBanner
        {
            get { return _ids != null && !StudioAppConfig.Blank(_ids.banner); }
        }

        public void SetBannerVisible(bool visible)
        {
            _bannerWanted = visible;
            if (!_initialized || !HasBanner) return;
            if (visible && _banner == null)
            {
                var position = _cfg.levelplay.bannerPosition == "top" ? LevelPlayBannerPosition.TopCenter : LevelPlayBannerPosition.BottomCenter;
                _banner = new LevelPlayBannerAd(_ids.banner.Trim(), new LevelPlayBannerAd.Config.Builder().SetPosition(position).Build());
                _banner.OnAdLoaded += info => Main(() => { if (!_bannerLoggedFill) { _bannerLoggedFill = true; _ev.Loaded(Banner); } });
                _banner.OnAdLoadFailed += err => Main(() => { if (!_bannerLoggedFail) { _bannerLoggedFail = true; _ev.LoadFailed(Banner, Code(err), 1); } });
                _banner.OnAdClicked += info => Main(() => _ev.Clicked(Banner, null));
                _banner.OnAdImpressionDataReady += Revenue;
                _banner.LoadAd();
            }
            if (_banner == null) return;
            if (visible) _banner.ShowAd();
            else _banner.HideAd();
        }

        // ------------------------------------------------------------------ shared

        /// <summary>Background thread: straight to the bridge (thread-safe; the auction id de-duplicates).</summary>
        static void Revenue(LevelPlayImpressionData data)
        {
            if (data == null) return;
            PulseAdRevenue r;
            if (PulseAdRevenueMap.FromLevelPlay(data.AdFormat, data.Revenue, data.Precision, data.AdNetwork,
                    data.MediationAdUnitName, data.Placement, data.AuctionId, out r))
            {
                PulseAdBridge.Report(r);
            }
        }

        void Retry(string format, PulseAdRetry retry, LevelPlayAdError err, Action load)
        {
            double delay = retry.Failed();
            _ev.LoadFailed(format, Code(err), retry.Attempt);
            StudioMainThread.Delay(delay, load);
        }

        static string Code(LevelPlayAdError err)
        {
            return err == null ? null : err.ErrorCode.ToString(System.Globalization.CultureInfo.InvariantCulture);
        }

        static void Main(Action a)
        {
            StudioMainThread.Run(a);
        }

        // LevelPlay has no consent form of its own: the "privacy settings" button reopens UMP's form.
        public bool PrivacyOptionsRequired
        {
            get { return StudioUmp.PrivacyOptionsRequired; }
        }

        public void ShowPrivacyOptions(Action done)
        {
            StudioUmp.ShowPrivacyOptions(done);
        }
    }
}
