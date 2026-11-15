// Pure C# (no UnityEngine).
using System;
using System.Collections.Generic;

namespace Studio.Pulse
{
    /// <summary>
    /// One ad impression's revenue, normalised for the ad_impression event. The ad SDK bridges
    /// (Studio.Pulse.AppLovinMax, Studio.Pulse.LevelPlay) and the Google Mobile Ads helper build
    /// it with <see cref="PulseAdRevenueMap"/>; docs/SDK_UNITY.md "Ad revenue".
    /// </summary>
    public struct PulseAdRevenue
    {
        /// <summary>The mediation platform: applovin_max, levelplay or admob.</summary>
        public string Platform;
        /// <summary>rewarded, interstitial, banner, mrec, app_open, rewarded_interstitial, native, or the SDK's own word.</summary>
        public string Format;
        /// <summary>Revenue of this impression in <see cref="Currency"/> (not micros, not CPM).</summary>
        public double Value;
        public string Currency;
        /// <summary>The network that served the ad (snake_case: applovin, google_admob, unity_ads ...).</summary>
        public string Source;
        public string AdUnit;
        public string Placement;
        /// <summary>The SDK's precision word, lower case (exact, estimated, publisher_defined, precise ...).</summary>
        public string Precision;
        /// <summary>
        /// The SDK's id of this impression when it has one (LevelPlay auction id). Not sent: two
        /// callbacks reporting the same impression (LevelPlay's global and per-ad events) count once.
        /// </summary>
        public string ImpressionId;

        public bool IsValid
        {
            get
            {
                return !string.IsNullOrEmpty(Platform) && !string.IsNullOrEmpty(Format) && !string.IsNullOrEmpty(Currency)
                    && !double.IsNaN(Value) && !double.IsInfinity(Value) && Value >= 0;
            }
        }
    }

    /// <summary>
    /// Maps each ad SDK's impression-level revenue callback to <see cref="PulseAdRevenue"/>.
    /// Pure functions, tested without Unity (unity/dotnet).
    /// </summary>
    public static class PulseAdRevenueMap
    {
        public const string PlatformMax = "applovin_max";
        public const string PlatformLevelPlay = "levelplay";
        public const string PlatformAdMob = "admob";

        /// <summary>
        /// AppLovin MAX: MaxSdkCallbacks.{Interstitial,Rewarded,Banner,MRec,AppOpen}.OnAdRevenuePaidEvent
        /// with MaxSdkBase.AdInfo. Revenue is in USD; the plugin reports -1 when it has none (skipped).
        /// </summary>
        /// <param name="callbackFormat">The format of the callback that fired (the bridge knows it).</param>
        public static bool FromMax(string callbackFormat, double revenue, string revenuePrecision, string networkName,
            string adUnitId, string placement, out PulseAdRevenue r)
        {
            r = new PulseAdRevenue
            {
                Platform = PlatformMax,
                Format = Format(callbackFormat),
                Value = revenue,
                Currency = "USD",
                Source = Source(networkName),
                AdUnit = Empty(adUnitId),
                Placement = Empty(placement),
                Precision = Lower(revenuePrecision),
            };
            return r.IsValid;
        }

        /// <summary>
        /// Unity LevelPlay (com.unity.services.levelplay 8.9+): LevelPlayImpressionData from the global
        /// LevelPlay.OnImpressionDataReady or an ad's OnAdImpressionDataReady (9.5+). Revenue is in
        /// USD; null means no revenue for this impression (skipped). The auction id de-duplicates.
        /// </summary>
        public static bool FromLevelPlay(string adFormat, double? revenue, string precision, string adNetwork,
            string mediationAdUnitName, string placement, string auctionId, out PulseAdRevenue r)
        {
            r = new PulseAdRevenue
            {
                Platform = PlatformLevelPlay,
                Format = Format(adFormat),
                Value = revenue ?? double.NaN,
                Currency = "USD",
                Source = Source(adNetwork),
                AdUnit = Empty(mediationAdUnitName),
                Placement = Empty(placement),
                Precision = Lower(precision),
                ImpressionId = Empty(auctionId),
            };
            return revenue.HasValue && r.IsValid;
        }

        /// <summary>
        /// Google Mobile Ads: the per-ad OnAdPaid event with AdValue (micros, ISO currency,
        /// PrecisionType 0 unknown, 1 estimated, 2 publisher provided, 3 precise).
        /// </summary>
        public static bool FromGoogleMobileAds(string adFormat, long valueMicros, string currencyCode, int precisionType,
            string adSourceName, string adUnitId, string placement, out PulseAdRevenue r)
        {
            r = new PulseAdRevenue
            {
                Platform = PlatformAdMob,
                Format = Format(adFormat),
                Value = valueMicros / 1000000.0,
                Currency = string.IsNullOrEmpty(currencyCode) ? null : currencyCode.Trim().ToUpperInvariant(),
                Source = Source(adSourceName),
                AdUnit = Empty(adUnitId),
                Placement = Empty(placement),
                Precision = GooglePrecision(precisionType),
            };
            return valueMicros >= 0 && r.Currency != null && r.Currency.Length == 3 && r.IsValid;
        }

        static string GooglePrecision(int p)
        {
            switch (p)
            {
                case 1: return "estimated";
                case 2: return "publisher_provided";
                case 3: return "precise";
                default: return "unknown";
            }
        }

        /// <summary>
        /// One word per format whatever the SDK calls it: MAX "INTER" / "REWARDED" / "APPOPEN",
        /// LevelPlay "Rewarded Video" / "interstitial", a game's "Rewarded" ... -> rewarded,
        /// interstitial, banner, mrec, app_open, rewarded_interstitial, native.
        /// </summary>
        public static string Format(string raw)
        {
            if (string.IsNullOrEmpty(raw)) return null;
            string k = Key(raw);
            if (k.Length == 0) return null;
            bool reward = k.Contains("reward"), inter = k.Contains("inter");
            if (reward && inter) return "rewarded_interstitial";
            if (reward) return "rewarded";
            if (inter) return "interstitial";
            if (k.Contains("mrec") || k.Contains("mediumrectangle")) return "mrec";
            if (k.Contains("banner") || k.Contains("leader")) return "banner";
            if (k.Contains("appopen")) return "app_open";
            if (k.Contains("native")) return "native";
            return Snake(raw, 40);
        }

        /// <summary>"Google AdMob" -> google_admob, "AppLovin" -> applovin, "Unity Ads" -> unity_ads.</summary>
        public static string Source(string networkName)
        {
            return string.IsNullOrEmpty(networkName) ? null : Snake(networkName, 60);
        }

        /// <summary>
        /// Which auto-captured platform an ad_platform string means, whatever the game wrote
        /// ("AppLovin", "applovin_max", "MAX", "ironSource", "LevelPlay" ...); null if none.
        /// </summary>
        public static string Family(string adPlatform)
        {
            if (string.IsNullOrEmpty(adPlatform)) return null;
            string k = Key(adPlatform);
            if (k.Contains("applovin") || k == "max" || k == "maxmediation") return PlatformMax;
            if (k.Contains("levelplay") || k.Contains("ironsource") || k == "unitymediation" || k == "unityadsmediation") return PlatformLevelPlay;
            if (k == "admob" || k == "googleadmob" || k == "googlemobileads" || k == "gma") return PlatformAdMob;
            return null;
        }

        /// <summary>Lower-case letters and digits only.</summary>
        static string Key(string s)
        {
            var b = new System.Text.StringBuilder(s.Length);
            foreach (char c0 in s)
            {
                char c = char.ToLowerInvariant(c0);
                if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) b.Append(c);
            }
            return b.ToString();
        }

        static string Snake(string s, int max)
        {
            var b = new System.Text.StringBuilder(s.Length);
            bool sep = false;
            foreach (char c0 in s.Trim())
            {
                char c = char.ToLowerInvariant(c0);
                if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9'))
                {
                    if (sep && b.Length > 0) b.Append('_');
                    b.Append(c);
                    sep = false;
                }
                else
                {
                    sep = true;
                }
                if (b.Length >= max) break;
            }
            return b.Length == 0 ? null : b.ToString();
        }

        static string Lower(string s)
        {
            return string.IsNullOrEmpty(s) ? null : s.Trim().ToLowerInvariant();
        }

        static string Empty(string s)
        {
            return string.IsNullOrEmpty(s) ? null : s;
        }
    }

    /// <summary>
    /// Keeps automatic and manual ad revenue from counting one impression twice, and holds
    /// impressions that arrive before Pulse.Init. Rules (docs/SDK_UNITY.md "Ad revenue"):
    /// <list type="number">
    /// <item>A manual Pulse.AdImpression for a platform a bridge captures (Register, auto-capture on) is dropped.</item>
    /// <item>Any other impression is dropped when the other path (automatic vs manual) recorded the
    /// same format and the same value within <see cref="WindowMs"/>: the same impression under another
    /// platform name. Each recorded impression cancels at most one from the other path.</item>
    /// </list>
    /// Thread-safe (MAX raises revenue callbacks on a background thread).
    /// </summary>
    public sealed class PulseAdRevenueGate
    {
        public const int WindowMs = 10000;
        public const int RecentMax = 32;
        public const int PendingMax = 32;

        struct Seen
        {
            public bool Auto;
            public string Format;
            public double Value;
            public long AtMs;
        }

        readonly object _lock = new object();
        readonly List<string> _registered = new List<string>(2);
        readonly Seen[] _recent = new Seen[RecentMax];
        int _recentNext, _recentCount;
        readonly List<PulseAdRevenue> _pending = new List<PulseAdRevenue>(4);
        readonly string[] _impressionIds = new string[RecentMax];
        int _impressionNext;
        bool _autoCapture = true;

        /// <summary>Automatic impressions recorded.</summary>
        public long Auto { get; private set; }
        /// <summary>Manual Pulse.AdImpression calls recorded.</summary>
        public long Manual { get; private set; }
        /// <summary>Impressions dropped as double counts (manual for an auto-captured platform, or a value match).</summary>
        public long Duplicates { get; private set; }
        /// <summary>Automatic impressions dropped because auto-capture is off or the before-Init buffer was full.</summary>
        public long Ignored { get; private set; }

        /// <summary>PulseOptions.AutoCaptureAdRevenue: false makes the bridges' impressions ignored.</summary>
        public bool AutoCapture
        {
            get { lock (_lock) return _autoCapture; }
            set { lock (_lock) _autoCapture = value; }
        }

        /// <summary>A bridge subscribed to this platform's revenue callback.</summary>
        public void Register(string adPlatform)
        {
            string f = PulseAdRevenueMap.Family(adPlatform) ?? adPlatform;
            lock (_lock)
            {
                if (f != null && !_registered.Contains(f)) _registered.Add(f);
            }
        }

        /// <summary>True when manual impressions for this platform are dropped (a bridge captures it).</summary>
        public bool IsCaptured(string adPlatform)
        {
            string f = PulseAdRevenueMap.Family(adPlatform);
            lock (_lock) return _autoCapture && f != null && _registered.Contains(f);
        }

        /// <summary>An automatic impression: true to record it now.</summary>
        public bool AcceptAuto(PulseAdRevenue r, long nowMs)
        {
            if (!r.IsValid) return false;
            lock (_lock)
            {
                if (!_autoCapture) { Ignored++; return false; }
                if (MatchesLocked(false, r.Format, r.Value, nowMs)) { Duplicates++; return false; }
                RememberLocked(true, r.Format, r.Value, nowMs);
                Auto++;
                return true;
            }
        }

        /// <summary>A manual Pulse.AdImpression: true to record it; otherwise <paramref name="reason"/> says why not.</summary>
        public bool AcceptManual(string adPlatform, string adFormat, double value, long nowMs, out string reason)
        {
            reason = null;
            string format = PulseAdRevenueMap.Format(adFormat);
            lock (_lock)
            {
                string f = PulseAdRevenueMap.Family(adPlatform);
                if (_autoCapture && f != null && _registered.Contains(f))
                {
                    Duplicates++;
                    reason = "ad revenue from " + f + " is captured automatically; manual AdImpression ignored "
                        + "(PulseOptions.AutoCaptureAdRevenue = false to log it by hand)";
                    return false;
                }
                if (MatchesLocked(true, format, value, nowMs))
                {
                    Duplicates++;
                    reason = "AdImpression matches an automatically captured impression (same format and value); ignored";
                    return false;
                }
                RememberLocked(false, format, value, nowMs);
                Manual++;
                return true;
            }
        }

        /// <summary>
        /// False when an impression with the same ImpressionId was already reported (the same
        /// impression through a second callback); impressions without an id are always new.
        /// </summary>
        public bool FirstReport(PulseAdRevenue r)
        {
            if (string.IsNullOrEmpty(r.ImpressionId)) return true;
            lock (_lock)
            {
                if (Array.IndexOf(_impressionIds, r.ImpressionId) >= 0) { Duplicates++; return false; }
                _impressionIds[_impressionNext] = r.ImpressionId;
                _impressionNext = (_impressionNext + 1) % RecentMax;
                return true;
            }
        }

        /// <summary>Keeps an automatic impression that arrived before Pulse.Init (at most PendingMax).</summary>
        public void Hold(PulseAdRevenue r)
        {
            if (!r.IsValid) return;
            lock (_lock)
            {
                if (_pending.Count < PendingMax) _pending.Add(r);
                else Ignored++;
            }
        }

        /// <summary>The impressions held before Init, in order; the buffer is emptied.</summary>
        public List<PulseAdRevenue> TakePending()
        {
            lock (_lock)
            {
                var list = new List<PulseAdRevenue>(_pending);
                _pending.Clear();
                return list;
            }
        }

        /// <summary>A recent impression of the other path matches; it is consumed (one match each).</summary>
        bool MatchesLocked(bool auto, string format, double value, long nowMs)
        {
            for (int i = 0; i < _recentCount; i++)
            {
                Seen s = _recent[i];
                if (s.Auto != auto || s.Format == null) continue;
                if (nowMs - s.AtMs > WindowMs || nowMs < s.AtMs - WindowMs) continue;
                if (!string.Equals(s.Format, format, StringComparison.Ordinal)) continue;
                if (Math.Abs(s.Value - value) <= 1e-12 * Math.Max(1.0, Math.Abs(value)))
                {
                    _recent[i].Format = null;
                    return true;
                }
            }
            return false;
        }

        void RememberLocked(bool auto, string format, double value, long nowMs)
        {
            _recent[_recentNext] = new Seen { Auto = auto, Format = format, Value = value, AtMs = nowMs };
            _recentNext = (_recentNext + 1) % RecentMax;
            if (_recentCount < RecentMax) _recentCount++;
        }
    }
}
