// Compiled only when the AppsFlyer Unity plug-in 7.x is in the project (UPM package
// appsflyer-unity-plugin, Studio.Pulse.Ads.AppsFlyer.asmdef). Checked against plug-in 7.0.41:
// AppsFlyer.init(devKey, appID, gameObject), AppsFlyer.start(), AppsFlyer.enableTCFDataCollection,
// AppsFlyer.logAdRevenue(AFAdRevenueData, Dictionary<string, string>), all returning Awaitable.
// Started by StudioApp after the ads consent flow (ATT answered, TCF string written by UMP).
using System;
using System.Collections.Generic;
using AppsFlyerSDK;
using UnityEngine;
using UnityEngine.Scripting;

[assembly: AlwaysLinkAssembly]

namespace Studio.Pulse.Ads
{
    [Preserve]
    sealed class AppsFlyerMmp : IStudioMmp
    {
        [Preserve]
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterAssembliesLoaded)]
        static void Register()
        {
            StudioApp.RegisterMmp("appsflyer", () => new AppsFlyerMmp());
        }

        public string Name
        {
            get { return "appsflyer"; }
        }

        public bool IsConfigured(StudioAppConfig config, string platform)
        {
            return config.MmpConfigured(platform);
        }

        public void Start(StudioAppConfig config, string platform)
        {
            StartAsync(config.appsflyer.devKey.Trim(), platform == "ios" ? config.appsflyer.iosAppId.Trim() : null, config.appsflyer.debug);
        }

        static async void StartAsync(string devKey, string iosAppId, bool debug)
        {
            try
            {
                await AppsFlyer.init(devKey, iosAppId);
                if (debug) await AppsFlyer.enableDebug(true);
                // Read the IAB TCF string the consent flow (Google UMP) stored, in GDPR regions.
                await AppsFlyer.enableTCFDataCollection(true);
                await AppsFlyer.start();
            }
            catch (Exception ex)
            {
                Debug.LogWarning("[Studio.Ads] AppsFlyer start failed: " + ex.Message);
            }
        }

        public void AdRevenue(PulseAdRevenue r)
        {
            var data = new AFAdRevenueData(r.Source ?? "unknown", Mediation(r.Platform), r.Currency, r.Value);
            var extra = new Dictionary<string, string>();
            if (r.AdUnit != null) extra[AdRevenueScheme.AD_UNIT] = r.AdUnit;
            if (r.Format != null) extra[AdRevenueScheme.AD_TYPE] = r.Format;
            if (r.Placement != null) extra[AdRevenueScheme.PLACEMENT] = r.Placement;
            Log(data, extra);
        }

        static async void Log(AFAdRevenueData data, Dictionary<string, string> extra)
        {
            try { await AppsFlyer.logAdRevenue(data, extra); }
            catch (Exception ex) { Debug.LogWarning("[Studio.Ads] AppsFlyer ad revenue: " + ex.Message); }
        }

        static MediationNetwork Mediation(string platform)
        {
            switch (platform)
            {
                case PulseAdRevenueMap.PlatformMax: return MediationNetwork.ApplovinMax;
                case PulseAdRevenueMap.PlatformLevelPlay: return MediationNetwork.IronSource;
                case PulseAdRevenueMap.PlatformAdMob: return MediationNetwork.GoogleAdMob;
                default: return MediationNetwork.Custom;
            }
        }
    }
}
