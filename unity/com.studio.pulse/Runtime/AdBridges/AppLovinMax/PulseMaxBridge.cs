// Compiled only when AppLovin MAX is in the project: the UPM package com.applovin.mediation.ads
// (versionDefines in Studio.Pulse.AppLovinMax.asmdef) or Assets/MaxSdk imported as a
// .unitypackage (Editor/PulseAdSdkDefines.cs sets STUDIO_PULSE_MAX). Signatures checked
// against the AppLovin MAX Unity Plugin 8.6.6 (MaxSdkCallbacks.cs, MaxSdkBase.AdInfo).
using UnityEngine;
using UnityEngine.Scripting;

[assembly: AlwaysLinkAssembly]

namespace Studio.Pulse.AdBridges
{
    /// <summary>
    /// Forwards MAX's impression-level revenue (the global OnAdRevenuePaidEvent of every ad
    /// format) to ad_impression. No game code: it subscribes before the first scene loads.
    /// Off with PulseOptions.AutoCaptureAdRevenue = false or the STUDIO_PULSE_NO_AD_BRIDGES define.
    /// </summary>
    [Preserve]
    static class PulseMaxBridge
    {
        static bool _subscribed;

        [Preserve]
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterAssembliesLoaded)]
        static void Subscribe()
        {
            if (_subscribed) return;
            _subscribed = true;
            PulseAdBridge.Register(PulseAdRevenueMap.PlatformMax);
            // MAX raises these on a background thread by default; PulseAdBridge is thread-safe.
            MaxSdkCallbacks.Interstitial.OnAdRevenuePaidEvent += (id, info) => Forward("interstitial", id, info);
            MaxSdkCallbacks.Rewarded.OnAdRevenuePaidEvent += (id, info) => Forward("rewarded", id, info);
            MaxSdkCallbacks.Banner.OnAdRevenuePaidEvent += (id, info) => Forward("banner", id, info);
            MaxSdkCallbacks.MRec.OnAdRevenuePaidEvent += (id, info) => Forward("mrec", id, info);
            MaxSdkCallbacks.AppOpen.OnAdRevenuePaidEvent += (id, info) => Forward("app_open", id, info);
        }

        static void Forward(string format, string adUnitId, MaxSdkBase.AdInfo info)
        {
            if (info == null) return;
            PulseAdRevenue r;
            if (PulseAdRevenueMap.FromMax(format, info.Revenue, info.RevenuePrecision, info.NetworkName,
                    string.IsNullOrEmpty(info.AdUnitIdentifier) ? adUnitId : info.AdUnitIdentifier, info.Placement, out r))
            {
                PulseAdBridge.Report(r);
            }
        }
    }
}
