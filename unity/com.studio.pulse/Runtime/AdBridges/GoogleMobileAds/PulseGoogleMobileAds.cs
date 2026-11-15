// Compiled only when Google Mobile Ads is in the project: the UPM package com.google.ads.mobile
// (versionDefines in Studio.Pulse.GoogleMobileAds.asmdef) or Assets/GoogleMobileAds imported as a
// .unitypackage (Editor/PulseAdSdkDefines.cs sets STUDIO_PULSE_GMA). AdValue checked against
// googleads-mobile-unity 11.7.0 (GoogleMobileAds.Core: Value in micros, CurrencyCode, Precision).
using System;
using GoogleMobileAds.Api;

namespace Studio.Pulse
{
    /// <summary>
    /// Google Mobile Ads reports revenue only on each ad object's OnAdPaid event, so it cannot be
    /// captured globally. One line per ad after it loads:
    /// <code>rewardedAd.OnAdPaid += PulseGoogleMobileAds.Paid("rewarded", adUnitId);</code>
    /// Do not use it for ads mediated by MAX or LevelPlay: those bridges capture them already.
    /// </summary>
    public static class PulseGoogleMobileAds
    {
        /// <summary>A handler for OnAdPaid that records ad_impression with ad_platform "admob".</summary>
        /// <param name="adFormat">rewarded, interstitial, banner, app_open, rewarded_interstitial or native.</param>
        /// <param name="adSource">The serving network if you know it (ResponseInfo.GetLoadedAdapterResponseInfo().AdSourceName).</param>
        public static Action<AdValue> Paid(string adFormat, string adUnitId = null, string placement = null, string adSource = null)
        {
            return value => Log(value, adFormat, adUnitId, placement, adSource);
        }

        /// <summary>Records one paid event (micros are converted to currency units).</summary>
        public static void Log(AdValue value, string adFormat, string adUnitId = null, string placement = null, string adSource = null)
        {
            if (value == null) return;
            PulseAdRevenue r;
            if (PulseAdRevenueMap.FromGoogleMobileAds(adFormat, value.Value, value.CurrencyCode, (int)value.Precision,
                    adSource, adUnitId, placement, out r))
            {
                Pulse.AdImpression(r.Platform, r.Format, r.Value, r.Currency, r.Source, r.AdUnit, r.Placement, r.Precision);
            }
        }
    }
}
