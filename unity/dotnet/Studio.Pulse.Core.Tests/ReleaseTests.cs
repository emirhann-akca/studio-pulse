using System;
using System.Collections.Generic;
using System.IO;
using System.Text.RegularExpressions;

namespace Studio.Pulse.Tests
{
    /// <summary>
    /// The release build's rules (StudioAppManifest.ApplyTo, PulseEnvFile, PulseStudioPaths): the
    /// game's studio-app.json + the studio's .env -> the release studio_app.json. Shaped like Acorn
    /// Mart's files (its ids replaced by made-up ones). The Editor step around it (StudioAppRelease:
    /// JSON, apply / restore, the build hooks) is tested in Unity: Tests/Editor/StudioAppReleaseTests.cs.
    /// </summary>
    public static class ReleaseTests
    {
        const string Env = "# acornmart: public, write-only collector key\nPULSE_COLLECTOR_URL=https://collector.example.run.app\nPULSE_WRITE_KEY=pk_acornmart_000000000000000000000000\n";

        /// <summary>A keyless config like the one in a game's repository (texts and switches set, no ids).</summary>
        static StudioAppConfig Keyless()
        {
            var c = new StudioAppConfig();
            c.privacy.privacyPolicyUrl = "https://example.com/game/privacy";
            c.privacy.attUsageDescription = new[] { new StudioAppConfig.LocalizedText { lang = "en", text = "Ads that fit you." } };
            c.levelplay.testSuite = false;
            c.levelplay.bannerPosition = "bottom";
            c.appsflyer.iosAppId = "";
            c.pacing.rewardedPerDay = 0;
            return c;
        }

        static StudioAppManifest Manifest()
        {
            var m = new StudioAppManifest { name = "Acorn Mart" };
            m.networks.levelplay.ios.app_key = "1a2b3c4d5";
            m.networks.levelplay.ios.ad_units.rewarded = "rw-ios-0001";
            m.networks.levelplay.ios.ad_units.interstitial = "is-ios-0001";
            m.networks.levelplay.android.app_key = "5d4c3b2a1";
            m.networks.levelplay.android.ad_units.rewarded = "rw-and-0001";
            m.networks.levelplay.android.ad_units.interstitial = "is-and-0001";
            m.networks.ump.ios_app_id = "ca-app-pub-1111111111111111~2222222222";
            m.networks.ump.android_app_id = "ca-app-pub-1111111111111111~3333333333";
            return m;
        }

        [PulseTest]
        public static void Release_ids_are_copied_in_and_nothing_else()
        {
            StudioAppConfig c = Manifest().ApplyTo(Keyless(), true, Env);
            Check.Equal("1a2b3c4d5", c.levelplay.iosAppKey, "iOS app key");
            Check.Equal("rw-ios-0001", c.levelplay.ios.rewarded, "iOS rewarded");
            Check.Equal("is-ios-0001", c.levelplay.ios.interstitial, "iOS interstitial");
            Check.Equal("", c.levelplay.ios.banner, "no banner");
            Check.Equal("5d4c3b2a1", c.levelplay.androidAppKey, "Android app key");
            Check.Equal("is-and-0001", c.levelplay.android.interstitial, "Android interstitial");
            Check.Equal("ca-app-pub-1111111111111111~2222222222", c.ump.iosAppId, "UMP iOS");
            Check.Equal("ca-app-pub-1111111111111111~3333333333", c.ump.androidAppId, "UMP Android");
            Check.Equal("https://collector.example.run.app", c.pulse.collectorUrl, "collector from the .env");
            Check.Equal("pk_acornmart_000000000000000000000000", c.pulse.writeKey, "write key from the .env");
            Check.Equal("levelplay", c.AdsProvider("ios"), "ads on, iOS");
            Check.Equal("levelplay", c.AdsProvider("android"), "ads on, Android");
            Check.True(c.PulseConfigured, "analytics on");
            Check.Equal(0, c.Problems("ios", true).Count, "nothing half-done: " + string.Join("; ", c.Problems("ios", true)));
            Check.Equal("https://example.com/game/privacy", c.privacy.privacyPolicyUrl, "privacy kept");
            Check.Equal("Ads that fit you.", c.AttText("en"), "ATT text kept");
            Check.Equal("", c.max.sdkKey, "MAX untouched");
            Check.Equal("", c.appsflyer.devKey, "AppsFlyer untouched");

            StudioAppConfig again = Manifest().ApplyTo(Manifest().ApplyTo(Keyless(), true, Env), true, Env);
            Check.Equal(c.levelplay.iosAppKey + c.levelplay.ios.rewarded + c.ump.androidAppId + c.pulse.writeKey,
                again.levelplay.iosAppKey + again.levelplay.ios.rewarded + again.ump.androidAppId + again.pulse.writeKey, "applying twice changes nothing");
        }

        [PulseTest]
        public static void Analytics_only_leaves_the_ads_alone()
        {
            StudioAppConfig c = Manifest().ApplyTo(Keyless(), false, Env);
            Check.Equal("", c.levelplay.iosAppKey, "no LevelPlay key");
            Check.Equal("", c.ump.iosAppId, "no UMP id");
            Check.True(c.AdsProvider("ios") == null && c.AdsProvider("android") == null, "ads off");
            Check.True(c.PulseConfigured, "analytics on");
        }

        [PulseTest]
        public static void Collector_comes_from_the_manifest_then_the_env_file()
        {
            var m = Manifest();
            m.networks.pulse.collectorUrl = "https://own.example.com";
            m.networks.pulse.writeKey = "pk_own";
            StudioAppConfig c = m.ApplyTo(Keyless(), true, Env);
            Check.Equal("https://own.example.com", c.pulse.collectorUrl, "the manifest wins");
            Check.Equal("pk_own", c.pulse.writeKey, "its key too");

            c = Manifest().ApplyTo(Keyless(), true, null);
            Check.True(!c.PulseConfigured, "no manifest values, no .env: analytics off");
            Check.Equal("", c.pulse.collectorUrl, "nothing invented");

            StudioAppConfig own = Keyless();
            own.pulse.collectorUrl = "https://kept.example.com";
            own.pulse.writeKey = "pk_kept";
            c = Manifest().ApplyTo(own, true, "# empty\n");
            Check.Equal("pk_kept", c.pulse.writeKey, "a config with its own keys keeps them when nothing replaces them");
        }

        [PulseTest]
        public static void A_platform_is_replaced_as_a_whole_and_absent_ones_are_kept()
        {
            StudioAppConfig before = Manifest().ApplyTo(Keyless(), true, Env);
            var m = new StudioAppManifest();
            m.networks.levelplay.ios.app_key = "9z9z9z9z9";
            m.networks.levelplay.ios.ad_units.rewarded = "rw-ios-0002";
            StudioAppConfig c = m.ApplyTo(before, true, null);
            Check.Equal("9z9z9z9z9", c.levelplay.iosAppKey, "new iOS app");
            Check.Equal("rw-ios-0002", c.levelplay.ios.rewarded, "its unit");
            Check.Equal("", c.levelplay.ios.interstitial, "a unit of the old app is not mixed in");
            Check.Equal("5d4c3b2a1", c.levelplay.androidAppKey, "Android not in this manifest: kept");
            Check.Equal("ca-app-pub-1111111111111111~2222222222", c.ump.iosAppId, "UMP not in this manifest: kept");
            Check.True(c.PulseConfigured, "collector kept");

            StudioAppConfig empty = new StudioAppManifest { networks = null }.ApplyTo(null, true, null);
            Check.True(empty.AdsProvider("ios") == null && !empty.PulseConfigured, "null sections: everything off");
        }

        [PulseTest]
        public static void Env_file_is_read_like_a_shell_would()
        {
            string url, key;
            Check.True(PulseEnvFile.Parse(Env, out url, out key), "make_keys.py file");
            Check.True(PulseEnvFile.Parse("\r\n# c\r\nexport PULSE_COLLECTOR_URL=\"https://a.example\"\r\nPULSE_WRITE_KEY='pk_x'\r\n", out url, out key), "CRLF, export, quotes");
            Check.Equal("https://a.example", url, "url");
            Check.Equal("pk_x", key, "key");
            Check.True(!PulseEnvFile.Parse("PULSE_COLLECTOR_URL=https://a.example\n", out url, out key), "a key is missing");
            Check.Equal("", key, "no key");
            Check.True(!PulseEnvFile.Parse(null, out url, out key), "no file");
            Check.Equal("collector.example.run.app", PulseEnvFile.Host("https://collector.example.run.app/"), "host only in logs");
            Check.Equal("invalid URL", PulseEnvFile.Host("not a url"), "bad URL");
        }

        [PulseTest]
        public static void Label_follows_the_key_rules_of_make_keys()
        {
            var rule = new Regex("^[a-z][a-z0-9_]{0,31}$"); // deploy/cloudrun/make_keys.py LABEL
            var cases = new Dictionary<string, string>
            {
                { "Acorn Mart", "acornmart" },
                { "Küp Dünyaları", "kupdunyalari" },
                { "Sandblok", "sandblok" },
                { "3D Çiçek Şehri 2", "dciceksehri2" },
                { "Isparta Iğdır İzmir", "ispartaigdirizmir" },
            };
            foreach (var kv in cases)
            {
                string label = new StudioAppManifest { name = kv.Key }.Label;
                Check.Equal(kv.Value, label, kv.Key);
                Check.True(rule.IsMatch(label), label + " is a make_keys label");
            }
            Check.Equal("acornmart", new StudioAppManifest { name = "Whatever", pulse_label = " AcornMart " }.Label, "pulse_label wins");
            Check.Equal(32, new StudioAppManifest { name = new string('a', 40) }.Label.Length, "at most 32");
            Check.Equal("", new StudioAppManifest { name = "123 !" }.Label, "no letter: no label");
        }

        [PulseTest]
        public static void Inject_mode_is_store_unless_manual()
        {
            Check.Equal("store", new StudioAppManifest().InjectMode, "default");
            Check.Equal("manual", new StudioAppManifest { release_inject = " Manual " }.InjectMode, "manual");
            Check.Equal("store", new StudioAppManifest { release_inject = "sometimes" }.InjectMode, "anything else");
        }

        [PulseTest]
        public static void Env_file_path_per_machine()
        {
            var vars = new Dictionary<string, string>();
            Func<string, string> env = k => vars.TryGetValue(k, out string v) ? v : null;
            string home = Path.Combine(Path.GetTempPath(), "home");
            var m = new StudioAppManifest { name = "Acorn Mart" };

            Check.Equal(Path.Combine(home, "Library", "Application Support", "studio-pulse", "sdk", "acornmart.env"),
                PulseStudioPaths.SdkEnvPath(m, "/repo", env, home, false, true), "macOS, the file Acorn Mart's release build read");
            Check.Equal(Path.Combine(home, ".local", "state", "studio-pulse", "sdk", "acornmart.env"),
                PulseStudioPaths.SdkEnvPath(m, "/repo", env, home, false, false), "Linux");
            Check.Equal(Path.Combine(home, "AppData", "Local", "studio-pulse", "sdk", "acornmart.env"),
                PulseStudioPaths.SdkEnvPath(m, "/repo", env, home, true, false), "Windows without LOCALAPPDATA");
            vars["XDG_STATE_HOME"] = "/xdg";
            Check.Equal(Path.Combine("/xdg", "studio-pulse", "sdk", "acornmart.env"), PulseStudioPaths.SdkEnvPath(m, "/repo", env, home, false, false), "XDG");
            vars["PULSE_HOME"] = "/pulse-home";
            Check.Equal(Path.Combine("/pulse-home", "sdk", "acornmart.env"), PulseStudioPaths.SdkEnvPath(m, "/repo", env, home, false, true), "PULSE_HOME");

            m.pulse_env = "keys/game.env";
            Check.Equal(Path.GetFullPath(Path.Combine("/repo", "keys/game.env")), PulseStudioPaths.SdkEnvPath(m, "/repo", env, home, false, true), "pulse_env, relative to the manifest");
            m.pulse_env = "~/secret/game.env";
            Check.Equal(Path.Combine(home, "secret/game.env"), PulseStudioPaths.SdkEnvPath(m, "/repo", env, home, false, true), "pulse_env with ~");
            vars[PulseStudioPaths.SdkEnvVariable] = "/override.env";
            Check.Equal("/override.env", PulseStudioPaths.SdkEnvPath(m, "/repo", env, home, false, true), "STUDIO_PULSE_SDK_ENV wins");
            vars.Remove(PulseStudioPaths.SdkEnvVariable);
            Check.True(PulseStudioPaths.SdkEnvPath(new StudioAppManifest(), "/repo", env, home, false, true) == null, "no label, no pulse_env: no file");
        }
    }
}
