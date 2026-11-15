// Build-time side of Resources/studio_app.json (docs/ADS_UNITY.md):
//  - logs what the config turns on and what is half-done, before every build;
//  - Android, ads off: removes the ad SDKs' start-up ContentProviders (AppLovin's reads the
//    advertising id at app start even when MAX is never initialised; ironSource / LevelPlay
//    register lifecycle trackers) and the AD_ID permission, so a build with empty keys behaves
//    like a build without the SDKs. Ads on with LevelPlay: AD_ID declared (Android 13+);
//  - iOS, ads on: NSUserTrackingUsageDescription, base English text plus every other language
//    (MAX localises en, de, es, fr, ja, ko, zh-Hans and zh-Hant itself, so only the rest for MAX).
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Xml.Linq;
using UnityEditor;
using UnityEditor.Android;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEditor.Callbacks;
using UnityEngine;

namespace Studio.Pulse.Editor
{
    public static class StudioAppConfigFile
    {
        public const string AssetPath = "Assets/Resources/studio_app.json";

        /// <summary>The project's config, or an empty one (everything off) when the file is missing.</summary>
        public static StudioAppConfig Read()
        {
            try
            {
                return File.Exists(AssetPath)
                    ? JsonUtility.FromJson<StudioAppConfig>(File.ReadAllText(AssetPath)) ?? new StudioAppConfig()
                    : new StudioAppConfig();
            }
            catch (Exception ex)
            {
                Debug.LogWarning("[Studio.Ads] " + AssetPath + " unreadable, building with everything off: " + ex.Message);
                return new StudioAppConfig();
            }
        }

        public static string PlatformOf(BuildTarget t)
        {
            return t == BuildTarget.iOS ? "ios" : t == BuildTarget.Android ? "android" : "other";
        }

        public static bool AssemblyLoaded(string name)
        {
            return AppDomain.CurrentDomain.GetAssemblies().Any(a => a.GetName().Name == name);
        }

        /// <summary>AppLovin MAX is in the project (UPM or .unitypackage).</summary>
        public static bool MaxInstalled
        {
            get { return AssemblyLoaded("MaxSdk.Scripts"); }
        }

        /// <summary>Unity LevelPlay is in the project.</summary>
        public static bool LevelPlayInstalled
        {
            get { return AssemblyLoaded("Unity.LevelPlay"); }
        }
    }

    sealed class StudioAppBuild : IPreprocessBuildWithReport, IPostGenerateGradleAndroidProject
    {
        const string AdId = "com.google.android.gms.permission.AD_ID";

        // Before the ad SDKs' own build steps (AppLovin uses int.MaxValue - 10).
        public int callbackOrder
        {
            get { return -100; }
        }

        public void OnPreprocessBuild(BuildReport report)
        {
            StudioAppConfig cfg = StudioAppConfigFile.Read();
            string platform = StudioAppConfigFile.PlatformOf(report.summary.platform);
            bool store = (report.summary.options & BuildOptions.Development) == 0;
            string provider = cfg.AdsProvider(platform);
            Debug.Log("[Studio.Ads] " + platform + " build: analytics " + (cfg.PulseConfigured ? "ON" : "off")
                + ", ads " + (provider ?? "off") + ", MMP " + (cfg.MmpConfigured(platform) ? "ON" : "off"));
            foreach (string p in cfg.Problems(platform, store)) Debug.LogWarning("[Studio.Ads] " + p);
            if (platform == "ios" && provider == StudioAppConfig.LevelPlay && cfg.AttWanted(provider) && cfg.AttText("en") != null)
            {
                string defines = PlayerSettings.GetScriptingDefineSymbols(NamedBuildTarget.iOS);
                if (!defines.Split(';').Select(d => d.Trim()).Contains("STUDIO_PULSE_ATT"))
                    Debug.LogWarning("[Studio.Ads] iOS ads with LevelPlay but STUDIO_PULSE_ATT is not defined: no tracking prompt, no IDFA (add it to the iOS Scripting Define Symbols)");
            }
        }

        public void OnPostGenerateGradleAndroidProject(string unityLibraryPath)
        {
            StudioAppConfig cfg = StudioAppConfigFile.Read();
            bool max = StudioAppConfigFile.MaxInstalled, levelplay = StudioAppConfigFile.LevelPlayInstalled;
            if (!max && !levelplay) return;
            string manifest = Path.Combine(unityLibraryPath, "..", "launcher", "src", "main", "AndroidManifest.xml");
            if (!File.Exists(manifest)) manifest = Path.Combine(unityLibraryPath, "src", "main", "AndroidManifest.xml");
            if (!File.Exists(manifest))
            {
                Debug.LogWarning("[Studio.Ads] AndroidManifest.xml not found; ad SDK start-up components left as they are");
                return;
            }
            string provider = cfg.AdsProvider("android");
            if (provider == null)
            {
                var providers = new List<string>();
                if (max) providers.Add("com.applovin.sdk.AppLovinInitProvider");
                if (levelplay)
                {
                    providers.Add("com.ironsource.lifecycle.IronsourceLifecycleProvider");
                    providers.Add("com.ironsource.lifecycle.LevelPlayActivityLifecycleProvider");
                }
                AndroidAdsOff(manifest, providers);
                Debug.Log("[Studio.Ads] ads off: removed " + string.Join(", ", providers) + " and AD_ID from " + manifest);
            }
            else if (provider == StudioAppConfig.LevelPlay)
            {
                AndroidDeclareAdId(manifest);
            }
        }

        static XNamespace Android
        {
            get { return "http://schemas.android.com/apk/res/android"; }
        }

        /// <summary>Adds tools:node="remove" for the given start-up providers and the AD_ID permission (idempotent).</summary>
        public static void AndroidAdsOff(string manifestPath, IEnumerable<string> providers)
        {
            XNamespace tools = "http://schemas.android.com/tools";
            XDocument doc = XDocument.Load(manifestPath);
            XElement root = doc.Root;
            if (root.Attribute(XNamespace.Xmlns + "tools") == null) root.Add(new XAttribute(XNamespace.Xmlns + "tools", tools.NamespaceName));
            XElement app = root.Element("application");
            if (app == null)
            {
                app = new XElement("application");
                root.Add(app);
            }
            foreach (string provider in providers)
            {
                if (app.Elements("provider").Any(e => (string)e.Attribute(Android + "name") == provider)) continue;
                app.Add(new XElement("provider",
                    new XAttribute(Android + "name", provider),
                    new XAttribute(tools + "node", "remove")));
            }
            XElement adId = root.Elements("uses-permission").FirstOrDefault(e => (string)e.Attribute(Android + "name") == AdId);
            if (adId == null) root.AddFirst(new XElement("uses-permission", new XAttribute(Android + "name", AdId), new XAttribute(tools + "node", "remove")));
            else adId.SetAttributeValue(tools + "node", "remove");
            doc.Save(manifestPath);
        }

        /// <summary>Declares AD_ID (needed for the advertising id on Android 13+; LevelPlay docs).</summary>
        public static void AndroidDeclareAdId(string manifestPath)
        {
            XDocument doc = XDocument.Load(manifestPath);
            XElement root = doc.Root;
            if (root.Elements("uses-permission").Any(e => (string)e.Attribute(Android + "name") == AdId)) return;
            root.AddFirst(new XElement("uses-permission", new XAttribute(Android + "name", AdId)));
            doc.Save(manifestPath);
        }

#if UNITY_IOS
        // MAX writes these itself from the Integration Manager (MaxSettingsSync fills them in).
        static readonly string[] MaxLanguages = { "en", "de", "es", "fr", "ja", "ko", "zh-hans", "zh-hant" };
        const string ResourcesDir = "StudioPulseResources";

        // After AppLovin's and LevelPlay's own post-processing.
        [PostProcessBuild(int.MaxValue - 5)]
        static void OnPostProcessIos(BuildTarget target, string buildPath)
        {
            if (target != BuildTarget.iOS) return;
            StudioAppConfig cfg = StudioAppConfigFile.Read();
            string provider = cfg.AdsProvider("ios");
            string en = cfg.AttText("en");
            if (provider == null || !cfg.AttWanted(provider) || en == null) return;

            if (provider == StudioAppConfig.LevelPlay)
            {
                string plistPath = Path.Combine(buildPath, "Info.plist");
                var plist = new UnityEditor.iOS.Xcode.PlistDocument();
                plist.ReadFromFile(plistPath);
                plist.root.SetString("NSUserTrackingUsageDescription", en);
                plist.WriteToFile(plistPath);
            }
            var project = new UnityEditor.iOS.Xcode.PBXProject();
            string projectPath = UnityEditor.iOS.Xcode.PBXProject.GetPBXProjectPath(buildPath);
            project.ReadFromFile(projectPath);
            string main = project.GetUnityMainTargetGuid();
            bool changed = false;
            foreach (StudioAppConfig.LocalizedText t in cfg.privacy.attUsageDescription)
            {
                if (t == null || StudioAppConfig.Blank(t.lang) || StudioAppConfig.Blank(t.text)) continue;
                string lang = t.lang.Trim();
                if (provider == StudioAppConfig.Max && MaxLanguages.Contains(lang.ToLowerInvariant())) continue;
                string rel = Path.Combine(ResourcesDir, lang + ".lproj");
                Directory.CreateDirectory(Path.Combine(buildPath, rel));
                string text = t.text.Trim().Replace("\\", "\\\\").Replace("\"", "\\\"");
                File.WriteAllText(Path.Combine(buildPath, rel, "InfoPlist.strings"),
                    "/* Generated by Studio Pulse from studio_app.json */\n\"NSUserTrackingUsageDescription\" = \"" + text + "\";\n");
                if (!project.ContainsFileByProjectPath(rel))
                {
                    string guid = project.AddFolderReference(rel, rel);
                    project.AddFileToBuild(main, guid);
                }
                changed = true;
            }
            if (changed) project.WriteToFile(projectPath);
        }
#endif
    }
}
