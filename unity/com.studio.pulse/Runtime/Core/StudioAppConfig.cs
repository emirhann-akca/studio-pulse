// Pure C# (no UnityEngine): the app's one configuration file and the ad rules that do not
// need Unity (retry delays, pacing). Loaded by Studio.Pulse.Ads from Resources/studio_app.json
// with JsonUtility; docs/ADS_UNITY.md. Ad providers: Unity LevelPlay (first choice) and
// AppLovin MAX; whichever section is complete for the platform is used.
using System;
using System.Collections.Generic;

namespace Studio.Pulse
{
    /// <summary>
    /// Everything an app needs to turn on analytics, ads and attribution, in one JSON file
    /// (Assets/Resources/studio_app.json). Every key defaults to empty: an empty file starts
    /// nothing, makes no network call and shows no ad.
    /// </summary>
    [Serializable]
    public sealed class StudioAppConfig
    {
        public PulseSection pulse = new PulseSection();
        public PrivacySection privacy = new PrivacySection();
        public LevelPlaySection levelplay = new LevelPlaySection();
        public MaxSection max = new MaxSection();
        public AppsFlyerSection appsflyer = new AppsFlyerSection();
        public UmpSection ump = new UmpSection();
        public PacingSection pacing = new PacingSection();

        [Serializable]
        public sealed class PulseSection
        {
            /// <summary>Collector base URL (https in store builds), e.g. https://collector.example.com.</summary>
            public string collectorUrl = "";
            /// <summary>Public write key from `pulse collect --key` (sp_pub_...).</summary>
            public string writeKey = "";
            /// <summary>Hold events in memory until Pulse.SetConsent(true).</summary>
            public bool consentRequired = false;
        }

        /// <summary>Shared by every ad provider.</summary>
        [Serializable]
        public sealed class PrivacySection
        {
            public string privacyPolicyUrl = "";
            public string termsOfServiceUrl = "";
            /// <summary>
            /// NSUserTrackingUsageDescription per language ("en" is the base text, required for the
            /// iOS ATT prompt). Written into the iOS build only when ads are on.
            /// </summary>
            public LocalizedText[] attUsageDescription = new LocalizedText[0];
            /// <summary>Ask ATT on iOS before the ad SDK starts (LevelPlay; MAX asks in its own flow).</summary>
            public bool requestAtt = true;
            /// <summary>
            /// GDPR consent to pass to the ad SDK when the game has no consent platform: "" (do not
            /// set; the SDK reads Google UMP / an Additional Consent CMP if one ran), "granted" or
            /// "denied". StudioAds.SetGdprConsent at run time overrides it.
            /// </summary>
            public string gdprConsent = "";
            /// <summary>The app is directed at children (COPPA): ad SDKs are told so.</summary>
            public bool childDirected = false;
        }

        [Serializable]
        public sealed class LevelPlaySection
        {
            /// <summary>LevelPlay app keys (dashboard: Apps). Empty: LevelPlay stays off on that platform.</summary>
            public string iosAppKey = "";
            public string androidAppKey = "";
            /// <summary>Ad unit ids (dashboard: Ad units), per platform.</summary>
            public AdUnitIds ios = new AdUnitIds();
            public AdUnitIds android = new AdUnitIds();
            /// <summary>Opens LevelPlay's Test Suite after init in development builds only.</summary>
            public bool testSuite = false;
            /// <summary>"bottom" or "top", when the game shows a banner.</summary>
            public string bannerPosition = "bottom";
        }

        [Serializable]
        public sealed class MaxSection
        {
            /// <summary>AppLovin SDK key (dashboard: Account > General > Keys). Empty: MAX stays off.</summary>
            public string sdkKey = "";
            public AdUnitIds ios = new AdUnitIds();
            public AdUnitIds android = new AdUnitIds();
            /// <summary>MAX Terms and Privacy Policy flow (ATT on iOS, Google UMP where set up); needs privacy.privacyPolicyUrl.</summary>
            public bool termsAndPrivacyFlow = true;
            /// <summary>MAX verbose logging (development only).</summary>
            public bool verboseLogging = false;
            /// <summary>Banner at the bottom of the screen when the game asks for one.</summary>
            public string bannerPosition = "bottom";
        }

        [Serializable]
        public sealed class AdUnitIds
        {
            public string rewarded = "";
            public string interstitial = "";
            public string banner = "";
        }

        [Serializable]
        public sealed class LocalizedText
        {
            public string lang = "";
            public string text = "";
        }

        [Serializable]
        public sealed class AppsFlyerSection
        {
            /// <summary>AppsFlyer dev key (dashboard: Configuration > App settings). Empty: no MMP.</summary>
            public string devKey = "";
            /// <summary>Numeric App Store id without "id" (needed on iOS only).</summary>
            public string iosAppId = "";
            public bool debug = false;
        }

        /// <summary>
        /// Google User Messaging Platform (GDPR / UK consent form) before the ad SDK starts. LevelPlay (7.7.0+)
        /// reads the consent UMP stores (IAB TCF + Google Additional Consent) by itself. Needs the native UMP
        /// SDK in the build and the STUDIO_PULSE_UMP define; empty ids skip it.
        /// </summary>
        [Serializable]
        public sealed class UmpSection
        {
            /// <summary>AdMob app id per platform (ca-app-pub-...~...), from AdMob > Apps > App settings.</summary>
            public string iosAppId = "";
            public string androidAppId = "";
            /// <summary>Development builds only: "eea", "other", "us_state" force the form's geography ("" = real).</summary>
            public string debugGeography = "";
            /// <summary>Development builds only: device ids UMP prints in the log, so debugGeography applies to them.</summary>
            public string[] testDeviceIds = new string[0];
        }

        /// <summary>Optional limits the Ads facade applies on top of the game's own (0 = no limit).</summary>
        [Serializable]
        public sealed class PacingSection
        {
            public int interstitialMinSeconds = 0;
            public int interstitialsPerDay = 0;
            public int rewardedPerDay = 0;
        }

        // ------------------------------------------------------------------ decisions

        public static bool Blank(string s)
        {
            return string.IsNullOrEmpty(s) || s.Trim().Length == 0;
        }

        /// <summary>The analytics SDK starts only with a collector URL and a write key.</summary>
        public bool PulseConfigured
        {
            get { return pulse != null && !Blank(pulse.collectorUrl) && !Blank(pulse.writeKey); }
        }

        public const string LevelPlay = "levelplay";
        public const string Max = "applovin_max";

        static bool AnyUnit(AdUnitIds u)
        {
            return u != null && (!Blank(u.rewarded) || !Blank(u.interstitial) || !Blank(u.banner));
        }

        /// <summary>The ad unit ids of a provider on "ios" or "android" (anything else: null).</summary>
        public AdUnitIds UnitsFor(string provider, string platform)
        {
            if (provider == LevelPlay && levelplay != null) return platform == "ios" ? levelplay.ios : platform == "android" ? levelplay.android : null;
            if (provider == Max && max != null) return platform == "ios" ? max.ios : platform == "android" ? max.android : null;
            return null;
        }

        /// <summary>The LevelPlay app key for the platform ("" when none).</summary>
        public string LevelPlayAppKey(string platform)
        {
            if (levelplay == null) return "";
            string k = platform == "ios" ? levelplay.iosAppKey : platform == "android" ? levelplay.androidAppKey : "";
            return Blank(k) ? "" : k.Trim();
        }

        /// <summary>LevelPlay starts only with the platform's app key and at least one ad unit id.</summary>
        public bool LevelPlayConfigured(string platform)
        {
            return LevelPlayAppKey(platform).Length > 0 && AnyUnit(UnitsFor(LevelPlay, platform));
        }

        /// <summary>MAX starts only with an SDK key and at least one ad unit id for this platform.</summary>
        public bool MaxConfigured(string platform)
        {
            return max != null && !Blank(max.sdkKey) && AnyUnit(UnitsFor(Max, platform));
        }

        /// <summary>The ad provider to start on this platform: LevelPlay first, then MAX, else null (no ads).</summary>
        public string AdsProvider(string platform)
        {
            if (LevelPlayConfigured(platform)) return LevelPlay;
            if (MaxConfigured(platform)) return Max;
            return null;
        }

        /// <summary>Some ad provider is complete for this platform.</summary>
        public bool AdsConfigured(string platform)
        {
            return AdsProvider(platform) != null;
        }

        /// <summary>AppsFlyer starts only with a dev key (and the App Store id on iOS).</summary>
        public bool MmpConfigured(string platform)
        {
            if (appsflyer == null || Blank(appsflyer.devKey)) return false;
            return platform != "ios" || !Blank(appsflyer.iosAppId);
        }

        /// <summary>What is wrong or half-done, for a warning in the log / build (never thrown).</summary>
        public List<string> Problems(string platform, bool storeBuild)
        {
            var p = new List<string>();
            if (pulse != null)
            {
                bool url = !Blank(pulse.collectorUrl), key = !Blank(pulse.writeKey);
                if (url != key) p.Add("pulse: collectorUrl and writeKey must both be set (analytics stays off)");
                if (url && storeBuild && !pulse.collectorUrl.Trim().StartsWith("https://", StringComparison.OrdinalIgnoreCase))
                    p.Add("pulse: collectorUrl must be https in a store build");
            }
            if (levelplay != null && (platform == "ios" || platform == "android"))
            {
                bool key = LevelPlayAppKey(platform).Length > 0, units = AnyUnit(UnitsFor(LevelPlay, platform));
                if (key && !units) p.Add("levelplay: the " + platform + " app key is set but no " + platform + " ad unit id (LevelPlay stays off)");
                if (!key && units) p.Add("levelplay: " + platform + " ad unit ids are set but the " + platform + " app key is empty (LevelPlay stays off)");
            }
            if (max != null && (platform == "ios" || platform == "android"))
            {
                bool units = AnyUnit(UnitsFor(Max, platform));
                if (!Blank(max.sdkKey) && !units) p.Add("max: sdkKey is set but no " + platform + " ad unit id (MAX stays off)");
                if (Blank(max.sdkKey) && units) p.Add("max: " + platform + " ad unit ids are set but sdkKey is empty (MAX stays off)");
            }
            string provider = AdsProvider(platform);
            if (provider == Max && max.termsAndPrivacyFlow && (privacy == null || Blank(privacy.privacyPolicyUrl)))
                p.Add("max: termsAndPrivacyFlow needs privacy.privacyPolicyUrl");
            if (provider != null && platform == "ios" && AttWanted(provider) && Blank(AttText("en")))
                p.Add("privacy: the iOS tracking prompt needs an English attUsageDescription (no ATT prompt without it)");
            if (privacy != null && !Blank(privacy.gdprConsent) && privacy.gdprConsent.Trim() != "granted" && privacy.gdprConsent.Trim() != "denied")
                p.Add("privacy: gdprConsent must be \"\", \"granted\" or \"denied\"");
            string umpId = UmpAppId(platform);
            if (umpId.Length > 0 && (!umpId.StartsWith("ca-app-pub-", StringComparison.Ordinal) || umpId.IndexOf('~') < 0))
                p.Add("ump: the " + platform + " AdMob app id looks wrong (ca-app-pub-...~...): " + umpId);
            if (umpId.Length > 0 && provider == Max)
                p.Add("ump: MAX runs its own consent flow; the ump section is used with LevelPlay only");
            if (appsflyer != null && !Blank(appsflyer.devKey) && platform == "ios" && Blank(appsflyer.iosAppId))
                p.Add("appsflyer: iosAppId is required on iOS (MMP stays off)");
            return p;
        }

        /// <summary>The AdMob app id UMP needs on this platform ("" when none).</summary>
        public string UmpAppId(string platform)
        {
            if (ump == null) return "";
            string id = platform == "ios" ? ump.iosAppId : platform == "android" ? ump.androidAppId : "";
            return Blank(id) ? "" : id.Trim();
        }

        /// <summary>UMP debug geography code for "eea" / "us_state" / "other" (0 = off; same numbers on iOS and Android).</summary>
        public static int UmpDebugGeography(string debugGeography)
        {
            switch ((debugGeography ?? "").Trim().ToLowerInvariant())
            {
                case "eea": return 1;
                case "us_state": return 3;
                case "other": return 4;
                default: return 0;
            }
        }

        /// <summary>The UMP consent step runs on this platform (an AdMob app id is set).</summary>
        public bool UmpConfigured(string platform)
        {
            return UmpAppId(platform).Length > 0;
        }

        /// <summary>The provider asks for ATT on iOS: MAX in its consent flow, LevelPlay through StudioAtt.</summary>
        public bool AttWanted(string provider)
        {
            if (provider == Max) return max != null && max.termsAndPrivacyFlow;
            if (provider == LevelPlay) return privacy != null && privacy.requestAtt;
            return false;
        }

        /// <summary>privacy.gdprConsent as a value to set before the ad SDK starts (null: do not set).</summary>
        public bool? GdprConsent
        {
            get
            {
                string c = privacy != null && !Blank(privacy.gdprConsent) ? privacy.gdprConsent.Trim() : "";
                return c == "granted" ? true : c == "denied" ? false : (bool?)null;
            }
        }

        /// <summary>The ATT usage text for a language ("en", "tr" ...), or null.</summary>
        public string AttText(string lang)
        {
            if (privacy == null || privacy.attUsageDescription == null) return null;
            foreach (LocalizedText t in privacy.attUsageDescription)
            {
                if (t != null && string.Equals(t.lang, lang, StringComparison.OrdinalIgnoreCase) && !Blank(t.text)) return t.text.Trim();
            }
            return null;
        }
    }

    /// <summary>AppLovin's recommended load retry: 2^min(6, attempt) seconds (2, 4 ... 64 s), reset on a load.</summary>
    public sealed class PulseAdRetry
    {
        public const int MaxExponent = 6;
        public int Attempt { get; private set; }

        /// <summary>A load failed: seconds to wait before the next load.</summary>
        public double Failed()
        {
            Attempt++;
            return Math.Pow(2, Math.Min(MaxExponent, Attempt));
        }

        public void Loaded()
        {
            Attempt = 0;
        }
    }

    /// <summary>
    /// The optional limits of StudioAppConfig.pacing: at most N per local day per format and a
    /// minimum gap between interstitials. Times are Unix seconds; the day boundary is the
    /// caller's (local midnight, passed as a day number). State is a plain struct to persist.
    /// </summary>
    public sealed class PulseAdPacing
    {
        [Serializable]
        public struct State
        {
            public long day;
            public int interstitials;
            public int rewarded;
            public long lastInterstitial;
        }

        public State Now;
        readonly StudioAppConfig.PacingSection _rules;

        public PulseAdPacing(StudioAppConfig.PacingSection rules, State state)
        {
            _rules = rules ?? new StudioAppConfig.PacingSection();
            Now = state;
        }

        void Roll(long day)
        {
            if (Now.day == day) return;
            Now.day = day;
            Now.interstitials = 0;
            Now.rewarded = 0;
        }

        public bool InterstitialAllowed(long unixSeconds, long day)
        {
            Roll(day);
            if (_rules.interstitialsPerDay > 0 && Now.interstitials >= _rules.interstitialsPerDay) return false;
            if (_rules.interstitialMinSeconds > 0 && Now.lastInterstitial > 0
                && unixSeconds - Now.lastInterstitial < _rules.interstitialMinSeconds) return false;
            return true;
        }

        public bool RewardedAllowed(long day)
        {
            Roll(day);
            return _rules.rewardedPerDay <= 0 || Now.rewarded < _rules.rewardedPerDay;
        }

        public void InterstitialShown(long unixSeconds, long day)
        {
            Roll(day);
            Now.interstitials++;
            Now.lastInterstitial = unixSeconds;
        }

        public void RewardedShown(long day)
        {
            Roll(day);
            Now.rewarded++;
        }
    }
}
