// Release keys for the build (docs/ADS_UNITY.md "Release keys", docs/YENI_OYUN_KURULUM.md).
//
// The project's Assets/Resources/studio_app.json stays KEYLESS in the repository: development, test
// and simulator builds start nothing (no network call, no consent form, no ATT prompt, no ad). The
// release build copies the ids in from the game's studio-app.json (the setup file next to the Unity
// project, see StudioAppManifest) and the collector key from the studio's local .env, and the file
// is written back byte for byte when the build ends:
//   networks.levelplay -> levelplay app keys + ad units      (ads)
//   networks.ump       -> ump app ids (Google UMP / AdMob)    (ads)
//   networks.pulse     -> pulse.collectorUrl / writeKey; empty: <state>/sdk/<label>.env (analytics)
// Values never enter the repository; logs show the collector's host only, never the key.
//
// When: release_inject "store" (the default) applies to every non-development build by itself;
// "manual" only to builds whose script calls StudioAppRelease.BuildPlayer (or Apply / Restore).
// STUDIO_APP_RELEASE=off turns the automatic step off (CI). The manifest is found through
// STUDIO_APP_MANIFEST, else the Unity project folder and up to three folders above it (stopping at
// the repository root). The file is restored by the build's last step, by the build script's finally,
// when the Editor quits and, after a crash, when the Editor loads again (a copy waits in Library/).
using System;
using System.IO;
using System.Linq;
using System.Text;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEngine;

namespace Studio.Pulse.Editor
{
    [InitializeOnLoad]
    public static class StudioAppRelease
    {
        public const string ManifestVariable = "STUDIO_APP_MANIFEST";
        public const string SwitchVariable = "STUDIO_APP_RELEASE";
        const string Tag = "[Studio.App] ";

        /// <summary>Where the original config waits while the release ids are in (survives a crash).</summary>
        public const string BackupPath = "Library/StudioPulse/studio_app.json.orig";
        const string AbsentMarker = "Library/StudioPulse/studio_app.json.absent";
        const string CreatedFolder = "the build created Assets/Resources too";

        static bool _applied;
        static bool _auto;

        static StudioAppRelease()
        {
            EditorApplication.quitting += Restore;
            if (Leftover) EditorApplication.delayCall += RestoreLeftover;
        }

        /// <summary>The config the release build writes (Studio.Ads reads it from Resources).</summary>
        public static string ConfigPath
        {
            get { return StudioAppConfigFile.AssetPath; }
        }

        /// <summary>The Unity project folder (the one with Assets/).</summary>
        public static string ProjectDirectory
        {
            get { return Path.GetFullPath(Path.Combine(Application.dataPath, "..")); }
        }

        /// <summary>The release ids are in the config now (this Editor session).</summary>
        public static bool Applied
        {
            get { return _applied; }
        }

        /// <summary>A copy of the keyless config waits in Library/ (applied now, or a build crashed).</summary>
        public static bool RestorePending
        {
            get { return Leftover; }
        }

        static bool Leftover
        {
            get { return File.Exists(BackupPath) || File.Exists(AbsentMarker); }
        }

        // ------------------------------------------------------------------ finding the files

        /// <summary>The game's studio-app.json, or null (see the file comment).</summary>
        public static string FindManifest()
        {
            return FindManifest(ProjectDirectory, Environment.GetEnvironmentVariable);
        }

        public static string FindManifest(string projectDirectory, Func<string, string> env)
        {
            string e = env(ManifestVariable);
            if (!string.IsNullOrEmpty(e)) return File.Exists(e) ? Path.GetFullPath(e) : null;
            string dir = projectDirectory;
            for (int up = 0; up <= 3 && !string.IsNullOrEmpty(dir); up++)
            {
                string p = Path.Combine(dir, StudioAppManifest.FileName);
                if (File.Exists(p)) return Path.GetFullPath(p);
                if (Directory.Exists(Path.Combine(dir, ".git")) || File.Exists(Path.Combine(dir, ".git"))) break;
                dir = Path.GetDirectoryName(dir);
            }
            return null;
        }

        public static StudioAppManifest ReadManifest(string path)
        {
            return JsonUtility.FromJson<StudioAppManifest>(File.ReadAllText(path)) ?? new StudioAppManifest();
        }

        static string Home
        {
            get
            {
                string h = Environment.GetEnvironmentVariable("HOME");
                return string.IsNullOrEmpty(h) ? Environment.GetFolderPath(Environment.SpecialFolder.UserProfile) : h;
            }
        }

        /// <summary>The collector .env of the game (PulseStudioPaths.SdkEnvPath on this machine), or null.</summary>
        public static string PulseEnvPath(StudioAppManifest manifest, string manifestPath)
        {
            return PulseStudioPaths.SdkEnvPath(manifest, Path.GetDirectoryName(manifestPath), Environment.GetEnvironmentVariable,
                Home, Application.platform == RuntimePlatform.WindowsEditor, Application.platform == RuntimePlatform.OSXEditor);
        }

        // ------------------------------------------------------------------ pure parts

        /// <summary>
        /// Config JSON + manifest JSON (+ the .env text, or null) -> the release config JSON
        /// (StudioAppManifest.ApplyTo; every other field as it was). Reads no file.
        /// </summary>
        public static string Merge(string configJson, string manifestJson, bool ads, string pulseEnvText)
        {
            var c = JsonUtility.FromJson<StudioAppConfig>(NoBom(configJson)) ?? new StudioAppConfig();
            var m = JsonUtility.FromJson<StudioAppManifest>(NoBom(manifestJson)) ?? new StudioAppManifest();
            return JsonUtility.ToJson(m.ApplyTo(c, ads, pulseEnvText), true);
        }

        static string NoBom(string s)
        {
            if (string.IsNullOrEmpty(s)) return "{}";
            return s[0] == '﻿' ? s.Substring(1) : s;
        }

        /// <summary>One log line: what is on, the collector's host only (never the key).</summary>
        public static string Summary(StudioAppConfig c)
        {
            return "ios=" + (c.AdsProvider("ios") ?? "off") + " android=" + (c.AdsProvider("android") ?? "off")
                + " ump=" + (c.UmpConfigured("ios") ? "ios " : "") + (c.UmpConfigured("android") ? "android" : "")
                + " analytics=" + (c.PulseConfigured ? "on (" + PulseEnvFile.Host(c.pulse.collectorUrl) + ")" : "off");
        }

        // ------------------------------------------------------------------ apply / restore

        /// <summary>
        /// Writes the release ids into the config (ads = false: analytics only) and re-imports it.
        /// Returns the config written, or null when nothing was written (already applied, no manifest).
        /// Pair it with <see cref="Restore"/> in a finally, or use <see cref="BuildPlayer"/>.
        /// </summary>
        public static StudioAppConfig Apply(bool ads = true)
        {
            return Apply(ads, FindManifest(), false);
        }

        static StudioAppConfig Apply(bool ads, string manifestPath, bool auto)
        {
            if (_applied) return null;
            if (Leftover)
            {
                // an earlier build did not restore (crash): put the keyless file back BEFORE copying it
                Debug.LogWarning(Tag + "an earlier build left the release ids in " + ConfigPath + ": restoring it first");
                Restore();
            }
            if (manifestPath == null)
            {
                Debug.LogWarning(Tag + "no " + StudioAppManifest.FileName + " (" + ManifestVariable + " or the project folder and above): the build has no release keys");
                return null;
            }
            StudioAppManifest manifest = ReadManifest(manifestPath);
            string envPath = PulseEnvPath(manifest, manifestPath);
            string envText = envPath != null && File.Exists(envPath) ? File.ReadAllText(envPath) : null;
            byte[] original = File.Exists(ConfigPath) ? File.ReadAllBytes(ConfigPath) : null;
            string merged = Merge(original != null ? Encoding.UTF8.GetString(original) : null, File.ReadAllText(manifestPath), ads, envText);

            Directory.CreateDirectory(Path.GetDirectoryName(BackupPath));
            if (original != null) File.WriteAllBytes(BackupPath, original);
            else File.WriteAllText(AbsentMarker, Directory.Exists(Path.GetDirectoryName(ConfigPath)) ? "" : CreatedFolder);
            _applied = true;
            _auto = auto;
            Directory.CreateDirectory(Path.GetDirectoryName(ConfigPath));
            File.WriteAllText(ConfigPath, merged);
            AssetDatabase.ImportAsset(ConfigPath, ImportAssetOptions.ForceUpdate);
            var cfg = JsonUtility.FromJson<StudioAppConfig>(merged);
            Debug.Log(Tag + "release ids written to " + ConfigPath + " (" + (ads ? "ads + analytics" : "analytics only") + ", "
                + Path.GetFileName(manifestPath) + (envText != null ? " + " + Path.GetFileName(envPath) : "") + "): " + Summary(cfg));
            if (!cfg.PulseConfigured)
                Debug.LogWarning(Tag + "analytics off: networks.pulse is empty and " + (envPath ?? "<state>/sdk/<label>.env") + " has no PULSE_COLLECTOR_URL / PULSE_WRITE_KEY");
            return cfg;
        }

        /// <summary>Writes the config back byte for byte (deletes it if the build created it). Safe to call twice.</summary>
        public static void Restore()
        {
            if (!_applied && !Leftover) return;
            _applied = false;
            _auto = false;
            bool restored = false;
            if (File.Exists(BackupPath))
            {
                File.WriteAllBytes(ConfigPath, File.ReadAllBytes(BackupPath));
                File.Delete(BackupPath);
                restored = true;
            }
            else if (File.Exists(AbsentMarker))
            {
                // through the AssetDatabase: deleting only the file would leave the keyed asset
                // loaded, and Resources.Load (and the next build) would still see it
                bool gone = false;
                try { gone = AssetDatabase.DeleteAsset(ConfigPath); }
                catch (Exception) { /* quitting */ }
                if (!gone)
                {
                    if (File.Exists(ConfigPath)) File.Delete(ConfigPath);
                    if (File.Exists(ConfigPath + ".meta")) File.Delete(ConfigPath + ".meta");
                }
                string folder = Path.GetDirectoryName(ConfigPath).Replace('\\', '/');
                if (File.ReadAllText(AbsentMarker) == CreatedFolder && Directory.Exists(folder)
                    && !Directory.EnumerateFileSystemEntries(folder).Any())
                {
                    try { AssetDatabase.DeleteAsset(folder); }
                    catch (Exception) { /* quitting */ }
                }
                File.Delete(AbsentMarker);
                Debug.Log(Tag + ConfigPath + " removed again (the build created it)");
                return;
            }
            if (!restored) return;
            try { AssetDatabase.ImportAsset(ConfigPath, ImportAssetOptions.ForceUpdate); }
            catch (Exception) { /* quitting: the next import picks it up */ }
            Debug.Log(Tag + ConfigPath + " restored (keyless)");
        }

        static void RestoreLeftover()
        {
            if (_applied || !Leftover) return;
            if (BuildPipeline.isBuildingPlayer)
            {
                EditorApplication.delayCall += RestoreLeftover;
                return;
            }
            Debug.LogWarning(Tag + "a build ended without restoring " + ConfigPath + " (crash?): restoring it now");
            Restore();
        }

        /// <summary>
        /// Builds with the release ids in (ads = false: analytics only, e.g. a simulator build that
        /// should send events) and restores the config afterwards, even when the build fails.
        /// </summary>
        public static BuildReport BuildPlayer(BuildPlayerOptions options, bool ads = true)
        {
            Apply(ads);
            try { return BuildPipeline.BuildPlayer(options); }
            finally { Restore(); }
        }

        // ------------------------------------------------------------------ the automatic step

        /// <summary>Whether a build applies the ids by itself (and why not).</summary>
        public static bool AutoWanted(bool development, string manifestPath, StudioAppManifest manifest, string switchValue, out string why)
        {
            if (string.Equals((switchValue ?? "").Trim(), "off", StringComparison.OrdinalIgnoreCase)) { why = SwitchVariable + "=off"; return false; }
            if (manifestPath == null) { why = "no " + StudioAppManifest.FileName; return false; }
            if (manifest.InjectMode == StudioAppManifest.InjectManual) { why = "release_inject is manual (the build script calls StudioAppRelease)"; return false; }
            if (development) { why = "development build (keyless)"; return false; }
            why = "store build";
            return true;
        }

        internal static void BeforeBuild(BuildReport report)
        {
            if (_applied) return; // the build script applied them (BuildPlayer / Apply)
            bool development = (report.summary.options & BuildOptions.Development) != 0;
            string switchValue = Environment.GetEnvironmentVariable(SwitchVariable);
            string path = FindManifest();
            StudioAppManifest manifest = null;
            if (path != null && !string.Equals((switchValue ?? "").Trim(), "off", StringComparison.OrdinalIgnoreCase))
            {
                try { manifest = ReadManifest(path); }
                catch (Exception e)
                {
                    if (!development) throw new BuildFailedException(Tag + path + " unreadable: " + e.Message);
                    Debug.LogWarning(Tag + path + " unreadable (" + e.Message + "): the development build stays keyless");
                    return;
                }
            }
            string why;
            if (!AutoWanted(development, path, manifest, switchValue, out why))
            {
                if (path != null) Debug.Log(Tag + "release ids not applied: " + why);
                return;
            }
            Apply(true, path, true);
            // a failed build skips the post-process step: restore on the next Editor update
            EditorApplication.delayCall += RestoreAfterAuto;
        }

        internal static void AfterBuild()
        {
            if (_auto) Restore();
        }

        static void RestoreAfterAuto()
        {
            if (!_auto) return;
            if (BuildPipeline.isBuildingPlayer)
            {
                EditorApplication.delayCall += RestoreAfterAuto;
                return;
            }
            Restore();
        }

        // ------------------------------------------------------------------ Android dependencies

        /// <summary>
        /// EDM4U's Android resolver, synchronously (writes the ad SDKs' Dependencies.xml into
        /// mainTemplate.gradle). EDM cannot copy its template without Assets/Plugins/Android: made first.
        /// </summary>
        public static bool ResolveAndroidDependenciesSync()
        {
            try
            {
                Directory.CreateDirectory("Assets/Plugins/Android");
                AssetDatabase.Refresh();
                Type t = Type.GetType("GooglePlayServices.PlayServicesResolver, Google.JarResolver")
                    ?? AppDomain.CurrentDomain.GetAssemblies().Select(a => a.GetType("GooglePlayServices.PlayServicesResolver", false)).FirstOrDefault(x => x != null);
                if (t == null)
                {
                    Debug.LogError(Tag + "EDM4U (GooglePlayServices.PlayServicesResolver) not found");
                    return false;
                }
                var m = t.GetMethod("ResolveSync", new[] { typeof(bool) });
                bool ok = m != null && (bool)m.Invoke(null, new object[] { true });
                AssetDatabase.SaveAssets();
                return ok;
            }
            catch (Exception e)
            {
                Debug.LogError(Tag + "Android dependencies: " + e);
                return false;
            }
        }

        /// <summary>-executeMethod Studio.Pulse.Editor.StudioAppRelease.ResolveAndroidDependencies (exit code 0 / 1 in batchmode).</summary>
        public static void ResolveAndroidDependencies()
        {
            bool ok = ResolveAndroidDependenciesSync();
            Debug.Log(Tag + "Android dependencies: " + (ok ? "OK" : "FAILED"));
            if (Application.isBatchMode) EditorApplication.Exit(ok ? 0 : 1);
        }
    }

    /// <summary>Before StudioAppBuild (-100) so its log and Android / iOS steps see the release ids.</summary>
    sealed class StudioAppReleaseBefore : IPreprocessBuildWithReport
    {
        public int callbackOrder
        {
            get { return -200; }
        }

        public void OnPreprocessBuild(BuildReport report)
        {
            StudioAppRelease.BeforeBuild(report);
        }
    }

    /// <summary>After every other post-process step (StudioAppBuild's iOS step is int.MaxValue - 5).</summary>
    sealed class StudioAppReleaseAfter : IPostprocessBuildWithReport
    {
        public int callbackOrder
        {
            get { return int.MaxValue; }
        }

        public void OnPostprocessBuild(BuildReport report)
        {
            StudioAppRelease.AfterBuild();
        }
    }
}
