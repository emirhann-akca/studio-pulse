using System;

namespace Studio.Pulse.Ads
{
    /// <summary>
    /// An ad mediation SDK behind <see cref="StudioAds"/>. Implementations live in optional
    /// assemblies compiled only when their SDK is in the project (Studio.Pulse.Ads.Max) and
    /// register themselves with <see cref="StudioApp.RegisterAdsProvider"/> at startup.
    /// Every member is called on the main thread; callbacks must come back on it too
    /// (<see cref="StudioMainThread.Post"/>).
    /// </summary>
    public interface IStudioAdsProvider
    {
        /// <summary>The ad_platform name (applovin_max ...).</summary>
        string Name { get; }

        /// <summary>
        /// Start the SDK (including its consent flow) for "ios" or "android" and begin loading.
        /// Call <paramref name="ready"/> once when the consent flow has finished (an MMP may start then),
        /// even if the SDK failed to initialise.
        /// </summary>
        void Initialize(StudioAppConfig config, string platform, IStudioAdsEvents events, Action ready);

        bool RewardedReady { get; }
        bool InterstitialReady { get; }
        bool HasBanner { get; }

        /// <summary>done(true) only when the SDK granted the reward; done(false) if not ready, failed or closed early.</summary>
        void ShowRewarded(string placement, Action<bool> done);

        /// <summary>done() when the ad closed or could not be shown.</summary>
        void ShowInterstitial(string placement, Action done);

        void SetBannerVisible(bool visible);

        /// <summary>True for users the consent flow says must be able to change their choice (GDPR regions).</summary>
        bool PrivacyOptionsRequired { get; }

        void ShowPrivacyOptions(Action done);
    }

    /// <summary>What a provider reports; StudioAds turns it into ad_event and availability changes.</summary>
    public interface IStudioAdsEvents
    {
        void LoadStarted(string format);
        void Loaded(string format);
        /// <param name="attempt">1 for the first failure in a row (later retries are not logged as events).</param>
        void LoadFailed(string format, string errorCode, int attempt);
        void Shown(string format, string placement);
        void ShowFailed(string format, string placement, string errorCode);
        void Clicked(string format, string placement);
        void Rewarded(string placement);
        void Closed(string format, string placement);
    }

    /// <summary>
    /// A mobile measurement partner (attribution SDK). Implementations live in optional assemblies
    /// compiled only when their SDK is present (Studio.Pulse.Ads.AppsFlyer) and register with
    /// <see cref="StudioApp.RegisterMmp"/>. Started after the ads consent flow.
    /// </summary>
    public interface IStudioMmp
    {
        string Name { get; }
        bool IsConfigured(StudioAppConfig config, string platform);
        void Start(StudioAppConfig config, string platform);
        /// <summary>One ad impression from the revenue bridges (main thread).</summary>
        void AdRevenue(PulseAdRevenue revenue);
    }
}
