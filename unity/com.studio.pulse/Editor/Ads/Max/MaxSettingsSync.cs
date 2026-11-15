// Compiled only when AppLovin MAX is in the project. Copies Resources/studio_app.json into the two
// places MAX reads at build time (checked against plug-in 8.6.6): AppLovinSettings (SDK key,
// Quality Service) and ProjectSettings/AppLovinInternalSettings.json (Terms and Privacy Policy
// flow, ATT usage text). studio_app.json stays the one place to edit; the Integration Manager
// shows the result. Empty SDK key: key cleared, Quality Service and the consent flow off, so a
// build without keys needs no network at build time and adds no ATT prompt.
using AppLovinMax.Scripts.IntegrationManager.Editor;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEngine;

namespace Studio.Pulse.Editor
{
    public sealed class MaxSettingsSync : IPreprocessBuildWithReport
    {
        // After StudioAppBuild (-100), before AppLovin's own pre-processing (int.MaxValue - 10).
        public int callbackOrder
        {
            get { return -90; }
        }

        public void OnPreprocessBuild(BuildReport report)
        {
            Apply(StudioAppConfigFile.Read());
        }

        [MenuItem("Tools/Studio Pulse/Apply studio_app.json to AppLovin settings")]
        public static void ApplyFromFile()
        {
            Apply(StudioAppConfigFile.Read());
        }

        public static void Apply(StudioAppConfig cfg)
        {
            bool ads = cfg.MaxConfigured("ios") || cfg.MaxConfigured("android");
            string key = ads ? cfg.max.sdkKey.Trim() : "";

            AppLovinSettings s = AppLovinSettings.Instance;
            if (s.SdkKey != key || s.QualityServiceEnabled != ads)
            {
                s.SdkKey = key;
                // The Quality Service (Ad Review) build plug-in needs a valid key: off without one.
                s.QualityServiceEnabled = ads;
                EditorUtility.SetDirty(s);
                AssetDatabase.SaveAssets();
            }

            string en = cfg.AttText("en");
            string policy = cfg.privacy != null && !StudioAppConfig.Blank(cfg.privacy.privacyPolicyUrl) ? cfg.privacy.privacyPolicyUrl.Trim() : null;
            string terms = cfg.privacy != null && !StudioAppConfig.Blank(cfg.privacy.termsOfServiceUrl) ? cfg.privacy.termsOfServiceUrl.Trim() : "";
            bool flow = ads && cfg.max.termsAndPrivacyFlow && policy != null && en != null;
            AppLovinInternalSettings i = AppLovinInternalSettings.Instance;
            if (!flow)
            {
                // The setter also clears the URLs and the ATT texts.
                i.ConsentFlowEnabled = false;
            }
            else
            {
                i.ConsentFlowEnabled = true;
                i.ConsentFlowPrivacyPolicyUrl = policy;
                i.ConsentFlowTermsOfServiceUrl = terms;
                i.OverrideDefaultUserTrackingUsageDescriptions = true;
                i.UserTrackingUsageLocalizationEnabled = true;
                i.UserTrackingUsageDescriptionEn = en;
                // Languages MAX localises itself; empty = not localised (iOS falls back to English).
                i.UserTrackingUsageDescriptionDe = cfg.AttText("de") ?? "";
                i.UserTrackingUsageDescriptionEs = cfg.AttText("es") ?? "";
                i.UserTrackingUsageDescriptionFr = cfg.AttText("fr") ?? "";
                i.UserTrackingUsageDescriptionJa = cfg.AttText("ja") ?? "";
                i.UserTrackingUsageDescriptionKo = cfg.AttText("ko") ?? "";
                i.UserTrackingUsageDescriptionZhHans = cfg.AttText("zh-Hans") ?? "";
                i.UserTrackingUsageDescriptionZhHant = cfg.AttText("zh-Hant") ?? "";
            }
            i.Save();
            Debug.Log("[Studio.Ads] AppLovin settings from studio_app.json: SDK key " + (ads ? "set" : "empty")
                + ", Quality Service " + (ads ? "on" : "off") + ", Terms and Privacy Policy flow " + (flow ? "on" : "off"));
        }
    }
}
