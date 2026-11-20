// EditMode tests of the package's Editor side, in their own Unity project (unity/editor-tests,
// README.md there) so a game that embeds the package never compiles them. The pure rules are also
// tested without Unity in unity/dotnet/Studio.Pulse.Core.Tests (ReleaseTests, AttemptTests). Every
// test that touches Assets/Resources/studio_app.json, PlayerPrefs or the defines puts them back.
using System;
using System.IO;
using System.Linq;
using NUnit.Framework;
using Studio.Pulse.Ads;
using UnityEngine;

namespace Studio.Pulse.Editor.Tests
{
    public class StudioAppReleaseTests
    {
        const string ConfigJson = @"{
  ""_help"": ""keyless, like the game's repository"",
  ""pulse"": { ""collectorUrl"": """", ""writeKey"": """", ""consentRequired"": false },
  ""privacy"": { ""privacyPolicyUrl"": ""https://example.com/game/privacy"",
    ""attUsageDescription"": [ { ""lang"": ""en"", ""text"": ""Ads that fit you."" } ], ""requestAtt"": true },
  ""levelplay"": { ""iosAppKey"": """", ""androidAppKey"": """", ""ios"": { ""rewarded"": """" }, ""android"": { ""rewarded"": """" }, ""testSuite"": false },
  ""ump"": { ""iosAppId"": """", ""androidAppId"": """", ""debugGeography"": """", ""testDeviceIds"": [] }
}";

        const string ManifestJson = @"{
  ""_help"": ""shaped like Acorn Mart's studio-app.json; made-up ids"",
  ""name"": ""Test Game"",
  ""platforms"": { ""ios"": { ""bundle_id"": ""com.example.testgame"" }, ""android"": { ""package_name"": ""com.example.testgame"" } },
  ""ad_formats"": [""rewarded"", ""interstitial""],
  ""networks"": {
    ""unity_ads"": { ""organization_core_id"": ""1"", ""ios"": { ""game_id"": ""2"" } },
    ""levelplay"": {
      ""ios"": { ""app_key"": ""1a2b3c4d5"", ""ad_units"": { ""rewarded"": ""rw-ios-0001"", ""interstitial"": ""is-ios-0001"" } },
      ""android"": { ""app_key"": ""5d4c3b2a1"", ""ad_units"": { ""rewarded"": ""rw-and-0001"", ""interstitial"": ""is-and-0001"" } }
    },
    ""ump"": { ""ios_app_id"": ""ca-app-pub-1111111111111111~2222222222"", ""android_app_id"": ""ca-app-pub-1111111111111111~3333333333"" },
    ""pulse"": { ""collectorUrl"": """", ""writeKey"": """" }
  },
  ""unity_config"": """"
}";

        const string Key = "pk_testgame_000000000000000000000000";
        const string EnvText = "PULSE_COLLECTOR_URL=https://collector.example.run.app\nPULSE_WRITE_KEY=" + Key + "\n";

        string _dir;
        string _manifestBefore, _envBefore, _switchBefore;

        [SetUp]
        public void SetUp()
        {
            _dir = Path.Combine(Path.GetTempPath(), "studio-app-release-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_dir);
            _manifestBefore = Environment.GetEnvironmentVariable(StudioAppRelease.ManifestVariable);
            _envBefore = Environment.GetEnvironmentVariable(PulseStudioPaths.SdkEnvVariable);
            _switchBefore = Environment.GetEnvironmentVariable(StudioAppRelease.SwitchVariable);
        }

        [TearDown]
        public void TearDown()
        {
            StudioAppRelease.Restore();
            Environment.SetEnvironmentVariable(StudioAppRelease.ManifestVariable, _manifestBefore);
            Environment.SetEnvironmentVariable(PulseStudioPaths.SdkEnvVariable, _envBefore);
            Environment.SetEnvironmentVariable(StudioAppRelease.SwitchVariable, _switchBefore);
            if (Directory.Exists(_dir)) Directory.Delete(_dir, true);
        }

        [Test]
        public void Merge_reads_both_files_with_JsonUtility()
        {
            var c = JsonUtility.FromJson<StudioAppConfig>(StudioAppRelease.Merge(ConfigJson, ManifestJson, true, EnvText));
            Assert.AreEqual("levelplay", c.AdsProvider("ios"));
            Assert.AreEqual("levelplay", c.AdsProvider("android"));
            Assert.AreEqual("is-and-0001", c.levelplay.android.interstitial);
            Assert.AreEqual("ca-app-pub-1111111111111111~2222222222", c.ump.iosAppId);
            Assert.IsTrue(c.PulseConfigured);
            Assert.AreEqual("Ads that fit you.", c.AttText("en"), "texts kept");
            Assert.AreEqual(0, c.Problems("ios", true).Count, string.Join("; ", c.Problems("ios", true)));

            var analytics = JsonUtility.FromJson<StudioAppConfig>(StudioAppRelease.Merge(ConfigJson, ManifestJson, false, EnvText));
            Assert.IsNull(analytics.AdsProvider("ios"), "analytics only: no ads");
            Assert.IsTrue(analytics.PulseConfigured);

            var bom = JsonUtility.FromJson<StudioAppConfig>(StudioAppRelease.Merge("﻿" + ConfigJson, ManifestJson, true, null));
            Assert.AreEqual("1a2b3c4d5", bom.levelplay.iosAppKey, "a BOM is tolerated");
            Assert.IsFalse(bom.PulseConfigured, "no .env: analytics off");
            string merged = StudioAppRelease.Merge(ConfigJson, ManifestJson, true, EnvText);
            Assert.AreEqual(merged, StudioAppRelease.Merge(merged, ManifestJson, true, EnvText), "applying twice is the same");
        }

        [Test]
        public void Summary_never_shows_the_write_key()
        {
            var c = JsonUtility.FromJson<StudioAppConfig>(StudioAppRelease.Merge(ConfigJson, ManifestJson, true, EnvText));
            string s = StudioAppRelease.Summary(c);
            Assert.AreEqual("ios=levelplay android=levelplay ump=ios android analytics=on (collector.example.run.app)", s);
            StringAssert.DoesNotContain(Key, s);
        }

        [Test]
        public void Manifest_is_found_next_to_the_project_up_to_the_repository_root()
        {
            string repo = Path.Combine(_dir, "repo"), project = Path.Combine(repo, "Unity", "Game");
            Directory.CreateDirectory(project);
            Directory.CreateDirectory(Path.Combine(repo, ".git"));
            Func<string, string> none = _ => null;
            Assert.IsNull(StudioAppRelease.FindManifest(project, none), "none yet");
            File.WriteAllText(Path.Combine(_dir, StudioAppManifest.FileName), "{}");
            Assert.IsNull(StudioAppRelease.FindManifest(project, none), "not above the repository root");
            File.WriteAllText(Path.Combine(repo, StudioAppManifest.FileName), "{}");
            Assert.AreEqual(Path.GetFullPath(Path.Combine(repo, StudioAppManifest.FileName)), StudioAppRelease.FindManifest(project, none), "the repository root (Acorn Mart's layout)");
            File.WriteAllText(Path.Combine(project, StudioAppManifest.FileName), "{}");
            Assert.AreEqual(Path.GetFullPath(Path.Combine(project, StudioAppManifest.FileName)), StudioAppRelease.FindManifest(project, none), "the project folder first");
            string other = Path.Combine(_dir, "other.json");
            File.WriteAllText(other, "{}");
            Assert.AreEqual(Path.GetFullPath(other), StudioAppRelease.FindManifest(project, k => k == StudioAppRelease.ManifestVariable ? other : null), "the variable wins");
            Assert.IsNull(StudioAppRelease.FindManifest(project, k => k == StudioAppRelease.ManifestVariable ? other + ".missing" : null), "a variable to a missing file finds nothing");
        }

        [Test]
        public void Automatic_step_runs_for_store_builds_only()
        {
            var store = new StudioAppManifest();
            var manual = new StudioAppManifest { release_inject = "manual" };
            string why;
            Assert.IsTrue(StudioAppRelease.AutoWanted(false, "/x/studio-app.json", store, null, out why), why);
            Assert.IsFalse(StudioAppRelease.AutoWanted(true, "/x/studio-app.json", store, null, out why), "development: " + why);
            Assert.IsFalse(StudioAppRelease.AutoWanted(false, "/x/studio-app.json", manual, null, out why), "manual: " + why);
            Assert.IsFalse(StudioAppRelease.AutoWanted(false, null, null, null, out why), "no manifest: " + why);
            Assert.IsFalse(StudioAppRelease.AutoWanted(false, "/x/studio-app.json", store, "OFF", out why), "switched off: " + why);
        }

        void UseFiles(bool withEnv)
        {
            string manifest = Path.Combine(_dir, StudioAppManifest.FileName);
            File.WriteAllText(manifest, ManifestJson);
            Environment.SetEnvironmentVariable(StudioAppRelease.ManifestVariable, manifest);
            string env = Path.Combine(_dir, "testgame.env");
            if (withEnv) File.WriteAllText(env, EnvText);
            Environment.SetEnvironmentVariable(PulseStudioPaths.SdkEnvVariable, env);
        }

        [Test]
        public void Apply_and_restore_give_back_the_same_bytes()
        {
            UseFiles(true);
            string path = StudioAppRelease.ConfigPath;
            bool existed = File.Exists(path);
            bool folder = Directory.Exists(Path.GetDirectoryName(path));
            byte[] before = existed ? File.ReadAllBytes(path) : null;
            try
            {
                StudioAppConfig c = StudioAppRelease.Apply();
                Assert.IsNotNull(c, "applied");
                Assert.IsTrue(StudioAppRelease.Applied);
                Assert.IsNull(StudioAppRelease.Apply(), "a second Apply does nothing");
                var onDisk = JsonUtility.FromJson<StudioAppConfig>(File.ReadAllText(path));
                Assert.AreEqual("levelplay", onDisk.AdsProvider("ios"), "the file carries the ids during the build");
                Assert.AreEqual(Key, onDisk.pulse.writeKey, "and the collector key");
                var loaded = StudioApp.LoadConfig();
                Assert.AreEqual("1a2b3c4d5", loaded.levelplay.iosAppKey, "Resources sees them (re-imported)");
                Assert.IsTrue(StudioAppRelease.RestorePending, "a copy (or a note that there was none) waits in Library/ in case of a crash");
            }
            finally
            {
                StudioAppRelease.Restore();
            }
            Assert.IsFalse(StudioAppRelease.Applied);
            Assert.IsFalse(StudioAppRelease.RestorePending, "the copy is gone");
            if (existed) CollectionAssert.AreEqual(before, File.ReadAllBytes(path), "byte for byte");
            else
            {
                Assert.IsFalse(File.Exists(path), "a config the build created is deleted again");
                Assert.IsFalse(StudioApp.LoadConfig().PulseConfigured, "and Resources no longer sees the keyed one");
                Assert.AreEqual(folder, Directory.Exists(Path.GetDirectoryName(path)), "a Resources folder the build made is gone too");
            }
            StudioAppRelease.Restore();
        }

        [Test]
        public void Analytics_only_build_has_no_ad_ids()
        {
            UseFiles(true);
            try
            {
                StudioAppConfig c = StudioAppRelease.Apply(false);
                Assert.IsNull(c.AdsProvider("ios"));
                Assert.IsNull(c.AdsProvider("android"));
                Assert.IsTrue(c.PulseConfigured);
            }
            finally
            {
                StudioAppRelease.Restore();
            }
        }

        [Test]
        public void Without_a_manifest_nothing_is_written()
        {
            Environment.SetEnvironmentVariable(StudioAppRelease.ManifestVariable, Path.Combine(_dir, "missing.json"));
            string path = StudioAppRelease.ConfigPath;
            byte[] before = File.Exists(path) ? File.ReadAllBytes(path) : null;
            Assert.IsNull(StudioAppRelease.Apply());
            Assert.IsFalse(StudioAppRelease.Applied);
            if (before != null) CollectionAssert.AreEqual(before, File.ReadAllBytes(path));
            else Assert.IsFalse(File.Exists(path));
        }

        [Test]
        public void Setup_check_changes_nothing()
        {
            UseFiles(true);
            string path = StudioAppRelease.ConfigPath;
            byte[] before = File.Exists(path) ? File.ReadAllBytes(path) : null;
            string ios = UnityEditor.PlayerSettings.GetScriptingDefineSymbols(UnityEditor.Build.NamedBuildTarget.iOS);
            var items = StudioAppSetup.Check(false);
            Assert.IsTrue(items.Any(i => i.Id == "manifest" && i.State == StudioAppSetup.State.Ok), string.Join("\n", items));
            Assert.IsFalse(items.Any(i => i.State == StudioAppSetup.State.Fixed), "a check fixes nothing");
            Assert.IsTrue(items.Any(i => i.Id == "collector" && i.State == StudioAppSetup.State.Ok), "the .env of the test is found");
            if (before != null) CollectionAssert.AreEqual(before, File.ReadAllBytes(path));
            Assert.AreEqual(ios, UnityEditor.PlayerSettings.GetScriptingDefineSymbols(UnityEditor.Build.NamedBuildTarget.iOS));
            var lines = new System.Collections.Generic.List<string>();
            int code = StudioAppSetup.Report(items, lines.Add);
            Assert.AreEqual(items.Any(i => i.State == StudioAppSetup.State.Todo) ? 1 : 0, code);
            StringAssert.StartsWith("[Studio.Setup] ", lines.Last());
        }

        [Test]
        public void Setup_makes_a_keyless_config_when_there_is_none()
        {
            string manifest = Path.Combine(_dir, StudioAppManifest.FileName);
            File.WriteAllText(manifest, "{ \"name\": \"Küp Dünyaları\", \"privacy_policy_url\": \"https://example.com/kup/privacy\", \"coppa\": false }");
            Environment.SetEnvironmentVariable(StudioAppRelease.ManifestVariable, manifest);
            string path = StudioAppRelease.ConfigPath;
            string aside = Path.Combine(_dir, "config.json");
            bool existed = File.Exists(path);
            bool folder = Directory.Exists(Path.GetDirectoryName(path));
            if (existed) File.Move(path, aside);
            try
            {
                var items = StudioAppSetup.Check(true);
                Assert.IsTrue(items.Any(i => i.Id == "config" && i.State == StudioAppSetup.State.Fixed), string.Join("\n", items));
                var c = JsonUtility.FromJson<StudioAppConfig>(File.ReadAllText(path));
                Assert.AreEqual("https://example.com/kup/privacy", c.privacy.privacyPolicyUrl);
                StringAssert.Contains("Küp Dünyaları", c.AttText("en"));
                StringAssert.StartsWith("Bu izin", c.AttText("tr"));
                Assert.AreEqual(0, StudioAppSetupRules.KeysIn(c).Count, "keyless");
                Assert.IsFalse(StudioApp.LoadConfig().PulseConfigured, "Resources sees a keyless config");
                var again = StudioAppSetup.Check(true);
                Assert.IsTrue(again.Any(i => i.Id == "config" && i.State == StudioAppSetup.State.Ok), "a second run changes nothing");
            }
            finally
            {
                if (existed)
                {
                    File.Delete(path); // its .meta stays: the original keeps its GUID
                    File.Move(aside, path);
                    UnityEditor.AssetDatabase.ImportAsset(path, UnityEditor.ImportAssetOptions.ForceUpdate);
                }
                else UnityEditor.AssetDatabase.DeleteAsset(path);
                if (!folder) UnityEditor.AssetDatabase.DeleteAsset(Path.GetDirectoryName(path).Replace('\\', '/'));
            }
        }

        [Test]
        public void Attempt_trace_choice_is_kept_per_install()
        {
            string key = PulseAttemptTrace.PrefsKey;
            bool had = PlayerPrefs.HasKey(key);
            int old = PlayerPrefs.GetInt(key, -1);
            try
            {
                Assert.AreEqual("pulse_iz_ornek", PulseAttemptTrace.DefaultPrefsKey, "Acorn Mart 1.0.1's key");
                PlayerPrefs.SetInt(key, 1);
                PulseAttemptTrace.Forget();
                Assert.IsTrue(PulseAttemptTrace.Sampled, "kept: in");
                PlayerPrefs.SetInt(key, 0);
                Assert.IsTrue(PulseAttemptTrace.Sampled, "cached for the process");
                PulseAttemptTrace.Forget();
                Assert.IsFalse(PulseAttemptTrace.Sampled, "kept: out");
                PlayerPrefs.DeleteKey(key);
                PulseAttemptTrace.Forget();
                bool chosen = PulseAttemptTrace.Sampled;
                Assert.AreEqual(chosen ? 1 : 0, PlayerPrefs.GetInt(key, -1), "the first choice is stored");
                PulseAttemptTrace.Force = !chosen;
                Assert.AreEqual(!chosen, PulseAttemptTrace.Sampled, "Force wins");
            }
            finally
            {
                PulseAttemptTrace.Force = null;
                PulseAttemptTrace.Forget();
                if (had) PlayerPrefs.SetInt(key, old);
                else PlayerPrefs.DeleteKey(key);
            }
        }

        [Test]
        public void Level_attempt_is_a_no_op_before_init()
        {
            Assert.IsNull(Pulse.Core, "these tests never start the SDK");
            string key = PulseAttemptTrace.PrefsKey;
            bool had = PlayerPrefs.HasKey(key);
            int old = PlayerPrefs.GetInt(key, -1);
            PlayerPrefs.DeleteKey(key);
            PulseAttemptTrace.Forget();
            try
            {
                Assert.IsFalse(Pulse.LevelAttempt(new PulseAttempt { Level = 1 }));
                Assert.IsFalse(PlayerPrefs.HasKey(key), "analytics off: the trace sample is not even chosen");
            }
            finally
            {
                if (had) PlayerPrefs.SetInt(key, old);
                PulseAttemptTrace.Forget();
            }
        }

        [Test]
        public void Launch_runs_the_game_hooks_once_and_ads_only_with_a_provider()
        {
            int started = 0, ads = 0;
            bool before = StudioApp.Launched;
            StudioApp.Launch(() => started++, () => ads++);
            StudioApp.Launch(() => started++, () => ads++);
            if (!before) Assert.AreEqual(1, started, "started once");
            Assert.LessOrEqual(started, 1, "never twice");
            Assert.AreEqual(0, ads, "no ad provider in this project: the game shows no ad button");
            Assert.IsTrue(StudioApp.Launched && StudioApp.Started);
            Assert.IsNull(Pulse.Core, "a keyless project starts no analytics");
        }
    }
}
