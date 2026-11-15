using System;
using System.Collections.Generic;
using System.Linq;

namespace Studio.Pulse.Tests
{
    /// <summary>
    /// The pure parts of the ad SDK bridges and the ads package: callback -> ad_impression mapping,
    /// the double-count gate, the config decisions, AppLovin's retry delays and the pacing rules.
    /// </summary>
    public static class AdRevenueTests
    {
        [PulseTest]
        public static void Max_impression_maps_to_ad_impression()
        {
            PulseAdRevenue r;
            Check.True(PulseAdRevenueMap.FromMax("rewarded", 0.0123, "EXACT", "Google AdMob", "abc123", "double_coins", out r), "valid");
            Check.Equal("applovin_max", r.Platform, "platform");
            Check.Equal("rewarded", r.Format, "format");
            Check.Equal(0.0123, r.Value, "value");
            Check.Equal("USD", r.Currency, "currency");
            Check.Equal("google_admob", r.Source, "source");
            Check.Equal("abc123", r.AdUnit, "ad unit");
            Check.Equal("double_coins", r.Placement, "placement");
            Check.Equal("exact", r.Precision, "precision");

            Check.True(!PulseAdRevenueMap.FromMax("interstitial", -1, "undefined", "AppLovin", "x", null, out r), "MAX reports -1 without revenue: skipped");
            Check.True(!PulseAdRevenueMap.FromMax("banner", double.NaN, null, null, null, null, out r), "NaN skipped");
            Check.True(PulseAdRevenueMap.FromMax("banner", 0, "", "", "", "", out r), "zero revenue is still an impression");
            Check.True(r.Source == null && r.AdUnit == null && r.Placement == null && r.Precision == null, "empty strings are not sent");
        }

        [PulseTest]
        public static void LevelPlay_impression_maps_to_ad_impression()
        {
            PulseAdRevenue r;
            Check.True(PulseAdRevenueMap.FromLevelPlay("Rewarded Video", 0.02, "BID", "ironSource", "Rewarded_Main", "Level_End", "auc-1", out r), "valid");
            Check.Equal("auc-1", r.ImpressionId, "auction id kept for de-duplication");
            Check.Equal("levelplay", r.Platform, "platform");
            Check.Equal("rewarded", r.Format, "format");
            Check.Equal("ironsource", r.Source, "source");
            Check.Equal("bid", r.Precision, "precision");
            Check.Equal("Rewarded_Main", r.AdUnit, "ad unit keeps its case");
            Check.True(!PulseAdRevenueMap.FromLevelPlay("interstitial", null, null, "x", null, null, null, out r), "no revenue: skipped");
            Check.True(!PulseAdRevenueMap.FromLevelPlay("interstitial", -0.5, null, "x", null, null, null, out r), "negative: skipped");
        }

        [PulseTest]
        public static void Google_paid_event_maps_to_ad_impression()
        {
            PulseAdRevenue r;
            Check.True(PulseAdRevenueMap.FromGoogleMobileAds("Rewarded", 12345, "eur", 3, "AdMob Network", "ca-app-pub-1/2", "shop", out r), "valid");
            Check.Equal("admob", r.Platform, "platform");
            Check.Equal(0.012345, r.Value, "micros -> currency units");
            Check.Equal("EUR", r.Currency, "currency upper case");
            Check.Equal("precise", r.Precision, "PrecisionType 3");
            Check.Equal("admob_network", r.Source, "source");
            string[] precision = { "unknown", "estimated", "publisher_provided", "precise", "unknown" };
            for (int p = 0; p <= 4; p++)
            {
                PulseAdRevenueMap.FromGoogleMobileAds("banner", 1, "USD", p, null, null, null, out r);
                Check.Equal(precision[p], r.Precision, "PrecisionType " + p);
            }
            Check.True(!PulseAdRevenueMap.FromGoogleMobileAds("banner", 1, "", 1, null, null, null, out r), "no currency: skipped");
            Check.True(!PulseAdRevenueMap.FromGoogleMobileAds("banner", 1, "EURO", 1, null, null, null, out r), "not ISO 4217: skipped");
            Check.True(!PulseAdRevenueMap.FromGoogleMobileAds("banner", -5, "USD", 1, null, null, null, out r), "negative micros: skipped");
        }

        [PulseTest]
        public static void Formats_and_platform_names_normalise()
        {
            var formats = new Dictionary<string, string>
            {
                ["INTER"] = "interstitial", ["Interstitial"] = "interstitial", ["REWARDED"] = "rewarded",
                ["rewarded_video"] = "rewarded", ["Rewarded Video"] = "rewarded", ["REWARDED_INTER"] = "rewarded_interstitial",
                ["BANNER"] = "banner", ["LEADER"] = "banner", ["MREC"] = "mrec", ["medium_rectangle"] = "mrec",
                ["APPOPEN"] = "app_open", ["App Open"] = "app_open", ["NATIVE"] = "native", ["Playable Thing"] = "playable_thing",
            };
            foreach (var kv in formats) Check.Equal(kv.Value, PulseAdRevenueMap.Format(kv.Key), "format " + kv.Key);
            Check.True(PulseAdRevenueMap.Format("") == null && PulseAdRevenueMap.Format("--") == null, "empty format");

            foreach (string s in new[] { "applovin_max", "AppLovin", "APPLOVIN MAX", "MAX", "max-mediation" })
                Check.Equal("applovin_max", PulseAdRevenueMap.Family(s), "family " + s);
            foreach (string s in new[] { "levelplay", "ironSource", "Unity LevelPlay", "ironsource_levelplay" })
                Check.Equal("levelplay", PulseAdRevenueMap.Family(s), "family " + s);
            foreach (string s in new[] { "admob", "Google AdMob", "google_mobile_ads" })
                Check.Equal("admob", PulseAdRevenueMap.Family(s), "family " + s);
            foreach (string s in new[] { "unity_ads", "house", "maxwell", null, "" })
                Check.True(PulseAdRevenueMap.Family(s) == null, "no family for '" + s + "'");
            Check.Equal("unity_ads", PulseAdRevenueMap.Source("  Unity Ads "), "source trimmed and snake case");
        }

        static PulseAdRevenue Max(string format, double value)
        {
            PulseAdRevenue r;
            PulseAdRevenueMap.FromMax(format, value, "exact", "AppLovin", "unit", null, out r);
            return r;
        }

        [PulseTest]
        public static void Manual_impression_for_a_captured_platform_is_ignored()
        {
            var gate = new PulseAdRevenueGate();
            string why;
            Check.True(gate.AcceptManual("AppLovin", "rewarded", 0.01, 0, out why), "nothing registered: manual counts");
            gate.Register(PulseAdRevenueMap.PlatformMax);
            Check.True(gate.IsCaptured("applovin_max") && gate.IsCaptured("MAX"), "captured");
            Check.True(!gate.AcceptManual("AppLovin", "rewarded", 0.02, 100, out why), "MAX is bridged: manual dropped");
            Check.True(why.Contains("captured automatically"), "reason given: " + why);
            Check.True(gate.AcceptManual("ironSource", "rewarded", 0.02, 100, out why), "another platform still counts");
            Check.True(gate.AcceptAuto(Max("rewarded", 0.03), 200), "automatic counts");
            Check.Equal(1L, gate.Auto, "auto");
            Check.Equal(2L, gate.Manual, "manual");
            Check.Equal(1L, gate.Duplicates, "duplicates");
        }

        [PulseTest]
        public static void Same_impression_under_another_name_counts_once()
        {
            var gate = new PulseAdRevenueGate();
            string why;
            Check.True(gate.AcceptAuto(Max("rewarded", 0.004567), 1000), "auto first");
            Check.True(!gate.AcceptManual("my_mediation", "Rewarded", 0.004567, 1500, out why), "manual repeat within 10 s dropped");
            Check.True(gate.AcceptManual("my_mediation", "Rewarded", 0.004567, 1000 + PulseAdRevenueGate.WindowMs + 1, out why), "after the window: a new impression");
            Check.True(gate.AcceptManual("my_mediation", "interstitial", 0.004567, 1600, out why), "other format: counts");
            Check.True(gate.AcceptManual("my_mediation", "rewarded", 0.004568, 1600, out why), "other value: counts");

            var g2 = new PulseAdRevenueGate();
            Check.True(g2.AcceptManual("custom", "interstitial", 0.5, 0, out why), "manual first");
            Check.True(!g2.AcceptAuto(Max("interstitial", 0.5), 3000), "auto repeat of the manual one dropped");
            Check.True(g2.AcceptAuto(Max("interstitial", 0.5), 3001), "the manual one cancels only one automatic impression");
            Check.True(g2.AcceptAuto(Max("interstitial", 0.5), 3002), "automatic impressions of the same value all count");
            Check.Equal(1L, g2.Duplicates, "one duplicate");
        }

        [PulseTest]
        public static void Auto_capture_off_hands_revenue_back_to_the_game()
        {
            var gate = new PulseAdRevenueGate();
            gate.Register("applovin_max");
            gate.AutoCapture = false;
            string why;
            Check.True(!gate.AcceptAuto(Max("rewarded", 0.01), 0), "bridge ignored");
            Check.True(!gate.IsCaptured("applovin_max"), "not captured");
            Check.True(gate.AcceptManual("applovin_max", "rewarded", 0.01, 0, out why), "manual counts");
            Check.Equal(1L, gate.Ignored, "ignored");
        }

        [PulseTest]
        public static void Impressions_before_init_are_held()
        {
            var gate = new PulseAdRevenueGate();
            for (int i = 0; i < PulseAdRevenueGate.PendingMax + 5; i++) gate.Hold(Max("banner", i / 1000.0));
            var held = gate.TakePending();
            Check.Equal(PulseAdRevenueGate.PendingMax, held.Count, "at most PendingMax held");
            Check.Equal(0.0, held[0].Value, "in order");
            Check.Equal(5L, gate.Ignored, "the rest counted");
            Check.Equal(0, gate.TakePending().Count, "emptied");
            gate.Hold(Max("banner", -1));
            Check.Equal(0, gate.TakePending().Count, "invalid not held");
        }

        [PulseTest]
        public static void Mapped_impression_reaches_the_collector()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                PulseAdRevenue r;
                PulseAdRevenueMap.FromMax("INTER", 0.0021, "estimated", "Unity Ads", "u1", "level_end", out r);
                core.AdImpression(r.Platform, r.Format, r.Value, r.Currency, r.Source, r.AdUnit, r.Placement, r.Precision);
                core.Flush();
                rig.Drain();
                var ev = rig.Collector.Rows.Single(x => (string)x["name"] == "ad_impression");
                var p = (Dictionary<string, object>)ev["p"];
                Check.Equal("applovin_max", (string)p["ad_platform"], "ad_platform");
                Check.Equal("interstitial", (string)p["ad_format"], "ad_format");
                Check.Equal(0.0021, Convert.ToDouble(p["value"]), "value");
                Check.Equal("USD", (string)p["currency"], "currency");
                Check.Equal("unity_ads", (string)p["ad_source"], "ad_source");
                Check.Equal("level_end", (string)p["placement"], "placement");
                Check.Equal("estimated", (string)p["precision"], "precision");
            }
        }

        [PulseTest]
        public static void Empty_config_turns_nothing_on()
        {
            var c = new StudioAppConfig();
            Check.True(!c.PulseConfigured, "no analytics");
            Check.True(!c.AdsConfigured("ios") && !c.AdsConfigured("android"), "no ads");
            Check.True(!c.MmpConfigured("ios") && !c.MmpConfigured("android"), "no MMP");
            Check.Equal(0, c.Problems("ios", true).Count + c.Problems("android", true).Count, "empty is not a problem");
            Check.True(!c.AdsConfigured("other"), "editor / desktop never");
        }

        [PulseTest]
        public static void Config_turns_on_only_complete_sections()
        {
            var c = new StudioAppConfig();
            c.max.sdkKey = "KEY";
            Check.True(!c.AdsConfigured("android"), "a key without ad units: off");
            Check.True(c.Problems("android", false).Any(p => p.Contains("no android ad unit")), "and said so");
            c.max.android.rewarded = " r1 ";
            Check.Equal("applovin_max", c.AdsProvider("android"), "MAX on android");
            Check.True(!c.AdsConfigured("ios"), "per platform");
            c.max.sdkKey = " ";
            Check.True(!c.AdsConfigured("android"), "blank key: off");
            Check.True(c.Problems("android", false).Any(p => p.Contains("sdkKey is empty")), "units without key reported");

            c.max.sdkKey = "KEY";
            c.max.ios.interstitial = "i1";
            Check.True(c.Problems("ios", true).Any(p => p.Contains("privacyPolicyUrl")), "the MAX flow needs a privacy URL");
            Check.True(c.Problems("ios", true).Any(p => p.Contains("English attUsageDescription")), "and ATT text on iOS");
            c.privacy.privacyPolicyUrl = "https://example.com/privacy";
            c.privacy.attUsageDescription = new[]
            {
                new StudioAppConfig.LocalizedText { lang = "EN", text = " Ads that fit you " },
                new StudioAppConfig.LocalizedText { lang = "tr", text = "Size uygun reklamlar" },
            };
            Check.Equal(0, c.Problems("ios", true).Count, "complete: " + string.Join("; ", c.Problems("ios", true)));
            Check.Equal("Ads that fit you", c.AttText("en"), "language match ignores case, text trimmed");
            Check.True(c.AttText("de") == null, "missing language");

            c.pulse.collectorUrl = "http://10.0.0.2:8787";
            Check.True(!c.PulseConfigured, "url alone: off");
            Check.True(c.Problems("ios", false).Any(p => p.Contains("must both be set")), "half set reported");
            c.pulse.writeKey = "sp_pub_x";
            Check.True(c.PulseConfigured, "url + key: on");
            Check.True(c.Problems("ios", true).Any(p => p.Contains("https")), "http in a store build reported");

            c.appsflyer.devKey = "dev";
            Check.True(c.MmpConfigured("android") && !c.MmpConfigured("ios"), "iOS needs the App Store id");
            c.appsflyer.iosAppId = "123";
            Check.True(c.MmpConfigured("ios"), "iOS on");
        }

        [PulseTest]
        public static void LevelPlay_comes_first_and_needs_key_and_unit_per_platform()
        {
            var c = new StudioAppConfig();
            c.levelplay.iosAppKey = "ios-key";
            Check.True(!c.LevelPlayConfigured("ios"), "app key without ad unit: off");
            Check.True(c.Problems("ios", false).Any(p => p.Contains("levelplay") && p.Contains("no ios ad unit")), "reported");
            c.levelplay.ios.rewarded = "rw1";
            Check.Equal("levelplay", c.AdsProvider("ios"), "LevelPlay on iOS");
            Check.True(c.AdsProvider("android") == null, "android has no key");
            c.levelplay.android.interstitial = "in1";
            Check.True(c.AdsProvider("android") == null, "android unit without the android app key: off");
            Check.True(c.Problems("android", false).Any(p => p.Contains("android app key is empty")), "reported");
            c.levelplay.androidAppKey = "and-key";
            Check.Equal("and-key", c.LevelPlayAppKey("android"), "per-platform app key");

            c.max.sdkKey = "KEY";
            c.max.ios.rewarded = "m1";
            Check.Equal("levelplay", c.AdsProvider("ios"), "both complete: LevelPlay wins");
            c.levelplay.iosAppKey = "";
            Check.Equal("applovin_max", c.AdsProvider("ios"), "MAX when LevelPlay is not set up");

            Check.True(c.AttWanted("levelplay") && c.AttWanted("applovin_max"), "ATT on by default");
            c.privacy.requestAtt = false;
            Check.True(!c.AttWanted("levelplay") && c.AttWanted("applovin_max"), "requestAtt is LevelPlay's switch; MAX asks in its flow");
            Check.True(c.GdprConsent == null, "gdprConsent empty: not set");
            c.privacy.gdprConsent = "denied";
            Check.True(c.GdprConsent == false, "denied");
            c.privacy.gdprConsent = " granted ";
            Check.True(c.GdprConsent == true, "granted");
            c.privacy.gdprConsent = "yes";
            Check.True(c.GdprConsent == null && c.Problems("ios", false).Any(p => p.Contains("gdprConsent")), "other words rejected");
        }

        [PulseTest]
        public static void One_impression_reported_twice_counts_once()
        {
            var gate = new PulseAdRevenueGate();
            PulseAdRevenue a, b, other, noId;
            PulseAdRevenueMap.FromLevelPlay("rewarded", 0.01, "bid", "ironSource", "rw", null, "auction-1", out a);
            PulseAdRevenueMap.FromLevelPlay("Rewarded Video", 0.01, "bid", "ironSource", "rw", null, "auction-1", out b);
            PulseAdRevenueMap.FromLevelPlay("rewarded", 0.01, "bid", "ironSource", "rw", null, "auction-2", out other);
            PulseAdRevenueMap.FromLevelPlay("rewarded", 0.01, "bid", "ironSource", "rw", null, null, out noId);
            Check.True(gate.FirstReport(a), "global event first");
            Check.True(!gate.FirstReport(b), "the per-ad event of the same auction: dropped");
            Check.True(gate.FirstReport(other), "another auction counts");
            Check.True(gate.FirstReport(noId) && gate.FirstReport(noId), "without an id nothing is dropped");
            Check.Equal(1L, gate.Duplicates, "one duplicate");
        }

        [PulseTest]
        public static void Retry_waits_like_applovin_recommends()
        {
            var r = new PulseAdRetry();
            double[] want = { 2, 4, 8, 16, 32, 64, 64, 64 };
            for (int i = 0; i < want.Length; i++) Check.Equal(want[i], r.Failed(), "attempt " + (i + 1));
            r.Loaded();
            Check.Equal(0, r.Attempt, "reset on load");
            Check.Equal(2.0, r.Failed(), "starts over");
        }

        [PulseTest]
        public static void Pacing_caps_per_day_and_spaces_interstitials()
        {
            var rules = new StudioAppConfig.PacingSection { interstitialMinSeconds = 120, interstitialsPerDay = 2, rewardedPerDay = 1 };
            var p = new PulseAdPacing(rules, default(PulseAdPacing.State));
            Check.True(p.InterstitialAllowed(1000, 10), "first");
            p.InterstitialShown(1000, 10);
            Check.True(!p.InterstitialAllowed(1100, 10), "100 s later: too soon");
            Check.True(p.InterstitialAllowed(1120, 10), "120 s later");
            p.InterstitialShown(1120, 10);
            Check.True(!p.InterstitialAllowed(5000, 10), "daily cap");
            Check.True(p.InterstitialAllowed(90000, 11), "next day");
            Check.True(p.RewardedAllowed(11), "rewarded");
            p.RewardedShown(11);
            Check.True(!p.RewardedAllowed(11), "rewarded cap per day");
            var restored = new PulseAdPacing(rules, p.Now);
            Check.True(!restored.RewardedAllowed(11), "state survives a restart");
            Check.True(p.RewardedAllowed(12), "the cap resets the next day");
            var none = new PulseAdPacing(new StudioAppConfig.PacingSection(), default(PulseAdPacing.State));
            for (int i = 0; i < 50; i++) none.InterstitialShown(i, 1);
            Check.True(none.InterstitialAllowed(50, 1) && none.RewardedAllowed(1), "zeros mean no limit");
        }
    }
}
