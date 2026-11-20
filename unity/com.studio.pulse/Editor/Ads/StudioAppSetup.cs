// One-call game setup (docs/YENI_OYUN_KURULUM.md): checks what a game needs for Studio Pulse
// analytics + LevelPlay ads and fixes what can be fixed without a person, idempotently:
//   - Assets/Resources/studio_app.json, KEYLESS, from the package sample: the privacy policy URL,
//     the en + tr tracking prompt texts and the COPPA flag from the game's studio-app.json;
//   - Google UMP's native SDK description (Samples~/AdsQuickStart/StudioPulseUmpDependencies.xml);
//   - the scripting defines (iOS STUDIO_PULSE_ATT + STUDIO_PULSE_UMP, Android STUDIO_PULSE_UMP).
// What it cannot do is listed as TODO: the packages (tools/new_game.py --unity adds them), LevelPlay's
// native SDK description (LevelPlay downloads it when the Editor is opened, not in batchmode) and
// the collector key on this machine (tools/new_game.py --apply).
//   Unity -batchmode -quit -projectPath <p> -executeMethod Studio.Pulse.Editor.StudioAppSetup.Run
//   (exit code 0: ready, 1: something left to do, 2: error); menu Tools > Studio Pulse.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEngine;

namespace Studio.Pulse.Editor
{
    public static class StudioAppSetup
    {
        public const string UmpDependenciesPath = "Assets/Plugins/StudioPulse/Editor/StudioPulseUmpDependencies.xml";
        public const string LevelPlayNativePath = "Assets/LevelPlay/Editor/IronSourceSDKDependencies.xml";
        const string Tag = "[Studio.Setup] ";

        public enum State { Ok, Fixed, Todo, Warn }

        public sealed class Item
        {
            public string Id;
            public State State;
            public string Message;

            public override string ToString()
            {
                return Tag + State.ToString().ToUpperInvariant().PadRight(5) + " " + Id + ": " + Message;
            }
        }

        /// <summary>The package's own folder on disk (for its Samples~ files), or null.</summary>
        public static string PackageDirectory
        {
            get
            {
                var info = UnityEditor.PackageManager.PackageInfo.FindForAssembly(typeof(StudioAppSetup).Assembly);
                if (info != null && Directory.Exists(info.resolvedPath)) return info.resolvedPath;
                string p = Path.GetFullPath("Packages/com.studio.pulse");
                return Directory.Exists(p) ? p : null;
            }
        }

        static string Sample(string name)
        {
            string dir = PackageDirectory;
            string p = dir != null ? Path.Combine(dir, "Samples~", "AdsQuickStart", name) : null;
            return p != null && File.Exists(p) ? p : null;
        }

        static bool TypeLoaded(string fullName)
        {
            return AppDomain.CurrentDomain.GetAssemblies().Any(a => a.GetType(fullName, false) != null);
        }

        /// <summary>EDM4U (External Dependency Manager) is in the project.</summary>
        public static bool Edm4uInstalled
        {
            get { return TypeLoaded("GooglePlayServices.PlayServicesResolver"); }
        }

        static bool UmpDependenciesPresent()
        {
            if (File.Exists(UmpDependenciesPath)) return true;
            if (!Directory.Exists("Assets")) return false;
            return Directory.GetFiles("Assets", "*Dependencies.xml", SearchOption.AllDirectories)
                .Any(f => File.ReadAllText(f).Contains("GoogleUserMessagingPlatform"));
        }

        /// <summary>Checks the setup; with <paramref name="fix"/> also fixes what it can. Never throws for a missing file.</summary>
        public static List<Item> Check(bool fix)
        {
            var items = new List<Item>();
            Action<string, State, string> add = (id, s, m) => items.Add(new Item { Id = id, State = s, Message = m });

            string manifestPath = StudioAppRelease.FindManifest();
            StudioAppManifest manifest = null;
            if (manifestPath == null)
                add("manifest", State.Todo, "no " + StudioAppManifest.FileName + " next to the project: python3 tools/new_game.py LABEL --name ... --ios ... --android ... --unity <project>");
            else
            {
                try
                {
                    manifest = StudioAppRelease.ReadManifest(manifestPath);
                    add("manifest", State.Ok, manifestPath + " (" + (manifest.Label.Length > 0 ? "label " + manifest.Label : "no label") + ", release builds: "
                        + (manifest.InjectMode == StudioAppManifest.InjectManual ? "manual, only StudioAppRelease.BuildPlayer" : "every non-development build") + ")");
                }
                catch (Exception e)
                {
                    add("manifest", State.Todo, manifestPath + " unreadable: " + e.Message);
                }
            }
            string name = manifest != null && !StudioAppConfig.Blank(manifest.name) ? manifest.name.Trim() : PlayerSettings.productName;
            string privacyUrl = manifest != null ? (manifest.privacy_policy_url ?? "").Trim() : "";

            // ---- the keyless config
            string cfgPath = StudioAppRelease.ConfigPath;
            if (!File.Exists(cfgPath))
            {
                if (fix)
                {
                    string sample = Sample("studio_app.json");
                    string text = sample != null ? StudioAppSetupRules.ConfigFromSample(File.ReadAllText(sample), name, privacyUrl, manifest != null && manifest.coppa) : null;
                    if (text == null) text = PlainConfig(name, privacyUrl, manifest != null && manifest.coppa);
                    Directory.CreateDirectory(Path.GetDirectoryName(cfgPath));
                    File.WriteAllText(cfgPath, text);
                    AssetDatabase.ImportAsset(cfgPath, ImportAssetOptions.ForceUpdate);
                    add("config", State.Fixed, cfgPath + " made, keyless (privacy URL, en + tr tracking prompt for \"" + name + "\")");
                }
                else add("config", State.Todo, cfgPath + " missing (the setup makes it)");
            }
            else if (fix && !StudioAppRelease.Applied)
            {
                string before = File.ReadAllText(cfgPath);
                string after = StudioAppSetupRules.FillMissing(before, name, privacyUrl);
                if (after != before.Replace("\r\n", "\n"))
                {
                    File.WriteAllText(cfgPath, after);
                    AssetDatabase.ImportAsset(cfgPath, ImportAssetOptions.ForceUpdate);
                    add("config", State.Fixed, cfgPath + ": empty privacy URL / tracking prompt filled in");
                }
            }
            if (File.Exists(cfgPath))
            {
                StudioAppConfig c = null;
                try { c = JsonUtility.FromJson<StudioAppConfig>(File.ReadAllText(cfgPath)); }
                catch (Exception e) { add("config", State.Todo, cfgPath + " is not valid JSON: " + e.Message); }
                if (c != null)
                {
                    if (!items.Any(i => i.Id == "config")) add("config", State.Ok, cfgPath);
                    var keys = StudioAppSetupRules.KeysIn(c);
                    if (keys.Count > 0 && !StudioAppRelease.Applied)
                        add("keyless", State.Warn, cfgPath + " carries " + string.Join(", ", keys) + ": keep ids in " + StudioAppManifest.FileName + " (networks) and the collector key in the studio's .env");
                    if (StudioAppConfig.Blank(c.privacy != null ? c.privacy.privacyPolicyUrl : null))
                        add("privacy", State.Todo, "privacy.privacyPolicyUrl is empty (set privacy_policy_url in " + StudioAppManifest.FileName + ", run the setup again)");
                    if (c.AttText("en") == null)
                        add("att-text", State.Todo, "privacy.attUsageDescription has no English text (no iOS tracking prompt without it)");
                }
            }

            // ---- ads: packages, native SDK descriptions, defines
            bool levelplay = StudioAppConfigFile.LevelPlayInstalled;
            if (!levelplay)
                add("levelplay", State.Todo, "Unity LevelPlay (com.unity.services.levelplay 9.x) is not installed: tools/new_game.py --unity adds it");
            else
            {
                add("levelplay", State.Ok, "Unity LevelPlay installed");
                if (!Edm4uInstalled)
                    add("edm4u", State.Todo, "External Dependency Manager (com.google.external-dependency-manager, OpenUPM) is not installed: tools/new_game.py --unity adds it");
                else add("edm4u", State.Ok, "External Dependency Manager installed");
                if (!File.Exists(LevelPlayNativePath))
                    add("levelplay-native", State.Todo, LevelPlayNativePath + " missing: open the project once in the Editor (not batchmode), LevelPlay downloads its native SDK description; commit Assets/LevelPlay/Editor");
                else add("levelplay-native", State.Ok, LevelPlayNativePath);

                if (!UmpDependenciesPresent())
                {
                    string src = Sample("StudioPulseUmpDependencies.xml");
                    if (fix && src != null)
                    {
                        Directory.CreateDirectory(Path.GetDirectoryName(UmpDependenciesPath));
                        File.Copy(src, UmpDependenciesPath, true);
                        AssetDatabase.ImportAsset(UmpDependenciesPath, ImportAssetOptions.ForceUpdate);
                        add("ump-sdk", State.Fixed, UmpDependenciesPath + " (Google UMP 3.1.0 iOS / 4.0.0 Android)");
                    }
                    else add("ump-sdk", State.Todo, UmpDependenciesPath + " missing (the setup copies it)");
                }
                else add("ump-sdk", State.Ok, "Google UMP dependencies present");

                bool ump = UmpDependenciesPresent();
                Defines(NamedBuildTarget.iOS, "iOS", fix, items, ump ? new[] { StudioAppSetupRules.AttDefine, StudioAppSetupRules.UmpDefine } : new[] { StudioAppSetupRules.AttDefine });
                if (ump) Defines(NamedBuildTarget.Android, "Android", fix, items, new[] { StudioAppSetupRules.UmpDefine });
                if (manifest != null && StudioAppConfig.Blank(manifest.networks.ump.ios_app_id) && StudioAppConfig.Blank(manifest.networks.ump.android_app_id))
                    add("ump-ids", State.Warn, "no AdMob app ids in " + StudioAppManifest.FileName + " networks.ump: no EU / UK consent form (AdMob > Apps > App settings)");
                if (manifest != null && StudioAppConfig.Blank(manifest.networks.levelplay.ios.app_key) && StudioAppConfig.Blank(manifest.networks.levelplay.android.app_key))
                    add("levelplay-ids", State.Warn, "no LevelPlay app keys in " + StudioAppManifest.FileName + ": release builds have no ads (tools/new_game.py with LEVELPLAY_SECRET_KEY / LEVELPLAY_REFRESH_TOKEN)");
            }

            // ---- the collector key on this machine
            if (manifest != null)
            {
                string env = StudioAppRelease.PulseEnvPath(manifest, manifestPath);
                string u, k;
                bool own = !StudioAppConfig.Blank(manifest.networks.pulse.collectorUrl) && !StudioAppConfig.Blank(manifest.networks.pulse.writeKey);
                if (own) add("collector", State.Ok, "networks.pulse in " + StudioAppManifest.FileName);
                else if (env != null && File.Exists(env) && PulseEnvFile.Parse(File.ReadAllText(env), out u, out k))
                    add("collector", State.Ok, env + " (" + PulseEnvFile.Host(u) + ")");
                else add("collector", State.Warn, (env ?? "<state>/sdk/<label>.env") + " not on this machine: release builds here have no analytics (tools/new_game.py --apply, or copy the file / set " + PulseStudioPaths.SdkEnvVariable + ")");
            }
            return items;
        }

        static void Defines(NamedBuildTarget target, string label, bool fix, List<Item> items, string[] wanted)
        {
            string have = PlayerSettings.GetScriptingDefineSymbols(target);
            string want = StudioAppSetupRules.AddDefines(have, wanted);
            var current = have.Split(';').Select(d => d.Trim()).ToList();
            if (wanted.All(current.Contains))
            {
                items.Add(new Item { Id = "defines-" + label.ToLowerInvariant(), State = State.Ok, Message = label + ": " + string.Join(", ", wanted) });
                return;
            }
            if (fix)
            {
                PlayerSettings.SetScriptingDefineSymbols(target, want);
                items.Add(new Item { Id = "defines-" + label.ToLowerInvariant(), State = State.Fixed, Message = label + " Scripting Define Symbols: " + want });
            }
            else items.Add(new Item { Id = "defines-" + label.ToLowerInvariant(), State = State.Todo, Message = label + " needs " + string.Join(", ", wanted.Where(w => !current.Contains(w))) });
        }

        static string PlainConfig(string name, string privacyUrl, bool child)
        {
            var c = new StudioAppConfig();
            c.privacy.privacyPolicyUrl = privacyUrl ?? "";
            c.privacy.childDirected = child;
            c.privacy.attUsageDescription = new[]
            {
                new StudioAppConfig.LocalizedText { lang = "en", text = StudioAppSetupRules.AttText("en", name) },
                new StudioAppConfig.LocalizedText { lang = "tr", text = StudioAppSetupRules.AttText("tr", name) },
            };
            return JsonUtility.ToJson(c, true);
        }

        /// <summary>The lines to print and the exit code (0 ready, 1 something to do).</summary>
        public static int Report(List<Item> items, Action<string> log)
        {
            foreach (Item i in items) log(i.ToString());
            int todo = items.Count(i => i.State == State.Todo);
            log(todo == 0 ? Tag + "READY" : Tag + "NOT READY: " + todo + " to do");
            return todo == 0 ? 0 : 1;
        }

        /// <summary>-executeMethod Studio.Pulse.Editor.StudioAppSetup.Run: fix what can be fixed, list the rest.</summary>
        public static void Run()
        {
            int code;
            try
            {
                code = Report(Check(true), Debug.Log);
                AssetDatabase.SaveAssets();
            }
            catch (Exception e)
            {
                Debug.LogError(Tag + "ERROR " + e);
                code = 2;
            }
            if (Application.isBatchMode) EditorApplication.Exit(code);
        }

        [MenuItem("Tools/Studio Pulse/Set up this game")]
        static void MenuSetUp()
        {
            Run();
        }

        [MenuItem("Tools/Studio Pulse/Check this game's setup")]
        static void MenuCheck()
        {
            Report(Check(false), Debug.Log);
        }
    }

    /// <summary>A store build logs what the setup still lists as missing (it never fails the build).</summary>
    sealed class StudioAppSetupBeforeBuild : IPreprocessBuildWithReport
    {
        public int callbackOrder
        {
            get { return -300; }
        }

        public void OnPreprocessBuild(BuildReport report)
        {
            if ((report.summary.options & BuildOptions.Development) != 0 || StudioAppRelease.FindManifest() == null) return;
            try
            {
                foreach (var i in StudioAppSetup.Check(false))
                    if (i.State == StudioAppSetup.State.Todo) Debug.LogWarning(i.ToString());
            }
            catch (Exception e)
            {
                Debug.LogWarning("[Studio.Setup] check failed: " + e.Message);
            }
        }
    }
}
