// Defines STUDIO_PULSE_MAX / _LEVELPLAY / _LEVELPLAY_ADS / _GMA / _APPSFLYER for the optional bridge and
// provider assemblies when an ad or attribution SDK was imported as a .unitypackage (into
// Assets/). UPM installs need nothing: the asmdefs' versionDefines set the same symbol from the
// package version. A symbol this script added is removed again when the SDK goes away.
using System;
using System.Collections.Generic;
using System.Linq;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Compilation;

namespace Studio.Pulse.Editor
{
    [InitializeOnLoad]
    public static class PulseAdSdkDefines
    {
        sealed class Sdk
        {
            public readonly string Define, Assembly, ApiType, ApiMember;

            public Sdk(string define, string assembly, string apiType, string apiMember = null)
            {
                Define = define;
                Assembly = assembly;
                ApiType = apiType;
                ApiMember = apiMember;
            }
        }

        // The vendor assembly (its .asmdef name) and a type the bridge needs: checked so an old
        // plug-in without that API does not get the symbol and break compilation.
        static readonly Sdk[] Sdks =
        {
            new Sdk("STUDIO_PULSE_MAX", "MaxSdk.Scripts", "MaxSdkCallbacks"),
            new Sdk("STUDIO_PULSE_LEVELPLAY", "Unity.LevelPlay", "Unity.Services.LevelPlay.LevelPlayImpressionData"),
            // The ads provider needs 9.5+ (LevelPlayPrivacySettings.SetGDPRConsent, per-ad revenue).
            new Sdk("STUDIO_PULSE_LEVELPLAY_ADS", "Unity.LevelPlay", "Unity.Services.LevelPlay.LevelPlayPrivacySettings", "SetGDPRConsent"),
            new Sdk("STUDIO_PULSE_GMA", "GoogleMobileAds.Core", "GoogleMobileAds.Api.AdValue"),
            // 7.x: AppsFlyer.init (6.x had initSDK and is not bridged).
            new Sdk("STUDIO_PULSE_APPSFLYER", "AppsFlyer", "AppsFlyerSDK.AppsFlyer", "init"),
        };

        static readonly NamedBuildTarget[] Targets = { NamedBuildTarget.Android, NamedBuildTarget.iOS, NamedBuildTarget.Standalone };

        static PulseAdSdkDefines()
        {
            EditorApplication.delayCall += Sync;
            CompilationPipeline.compilationFinished += _ => EditorApplication.delayCall += Sync;
        }

        [MenuItem("Tools/Studio Pulse/Refresh ad SDK defines")]
        public static void Sync()
        {
            try
            {
                var inAssets = new HashSet<string>();
                foreach (var a in CompilationPipeline.GetAssemblies(AssembliesType.PlayerWithoutTestAssemblies))
                {
                    if (a.sourceFiles.Length > 0 && a.sourceFiles[0].StartsWith("Assets/", StringComparison.Ordinal)) inAssets.Add(a.name);
                }
                foreach (NamedBuildTarget t in Targets)
                {
                    string before = PlayerSettings.GetScriptingDefineSymbols(t);
                    var symbols = new List<string>(before.Split(new[] { ';' }, StringSplitOptions.RemoveEmptyEntries).Select(s => s.Trim()));
                    foreach (Sdk sdk in Sdks)
                    {
                        bool want = inAssets.Contains(sdk.Assembly) && HasApi(sdk);
                        bool has = symbols.Contains(sdk.Define);
                        if (want && !has) symbols.Add(sdk.Define);
                        if (!want && has) symbols.Remove(sdk.Define);
                    }
                    string after = string.Join(";", symbols);
                    if (after != before) PlayerSettings.SetScriptingDefineSymbols(t, after);
                }
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning("[Studio.Pulse] ad SDK define check failed: " + ex.Message);
            }
        }

        /// <summary>The API type exists in the loaded assembly (true while it is not loaded yet).</summary>
        static bool HasApi(Sdk sdk)
        {
            var loaded = AppDomain.CurrentDomain.GetAssemblies().FirstOrDefault(x => x.GetName().Name == sdk.Assembly);
            if (loaded == null) return true;
            Type type = loaded.GetType(sdk.ApiType, false);
            return type != null && (sdk.ApiMember == null || type.GetMember(sdk.ApiMember).Length > 0);
        }
    }
}
