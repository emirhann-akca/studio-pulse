// Pure C# (no UnityEngine): the rules of the Editor's one-call game setup (StudioAppSetup):
// the keyless Resources/studio_app.json made from the sample, the default tracking prompt texts and
// the scripting define symbols. docs/YENI_OYUN_KURULUM.md.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;

namespace Studio.Pulse
{
    public static class StudioAppSetupRules
    {
        /// <summary>iOS: the tracking prompt (Plugins/iOS/Ads/StudioPulseAtt.mm).</summary>
        public const string AttDefine = "STUDIO_PULSE_ATT";
        /// <summary>iOS and Android: the Google UMP consent bridge (needs the UMP SDK dependencies).</summary>
        public const string UmpDefine = "STUDIO_PULSE_UMP";

        /// <summary>The default tracking prompt text (en, tr) for a game; null for other languages.</summary>
        public static string AttText(string lang, string gameName)
        {
            string name = string.IsNullOrEmpty(gameName) || gameName.Trim().Length == 0 ? "this game" : gameName.Trim();
            switch (lang)
            {
                case "en":
                    return "This lets the ads in " + name + " be more relevant to you. The game is free and plays the same whether you allow it or not.";
                case "tr":
                    return "Bu izin, bu oyundaki reklamların ilgi alanlarınıza daha uygun olmasını sağlar. " + name
                        + " ücretsizdir; izin verseniz de vermeseniz de aynı şekilde oynanır.";
                default:
                    return null;
            }
        }

        /// <summary>A JSON string literal (quotes included).</summary>
        public static string JsonString(string s)
        {
            var b = new StringBuilder("\"");
            foreach (char c in s ?? "")
            {
                switch (c)
                {
                    case '"': b.Append("\\\""); break;
                    case '\\': b.Append("\\\\"); break;
                    case '\n': b.Append("\\n"); break;
                    case '\r': b.Append("\\r"); break;
                    case '\t': b.Append("\\t"); break;
                    default:
                        if (c < 0x20) b.Append("\\u").Append(((int)c).ToString("x4"));
                        else b.Append(c);
                        break;
                }
            }
            return b.Append('"').ToString();
        }

        const string SampleAtt = "\"attUsageDescription\": [\n      {\n        \"lang\": \"en\",\n        \"text\": \"\"\n      }\n    ]";
        const string SampleUrl = "\"privacyPolicyUrl\": \"\"";
        const string SampleChild = "\"childDirected\": false";
        const string SampleHelp = "\"_help\": \"Copy to Assets/Resources/studio_app.json. ";

        /// <summary>
        /// The game's keyless config from the package's Samples~/AdsQuickStart/studio_app.json text (its
        /// comments kept): the privacy policy URL, the en + tr tracking prompt texts and the COPPA flag
        /// filled in, every key left empty. Null when the sample is not the expected one (the caller then
        /// writes a plain config).
        /// </summary>
        public static string ConfigFromSample(string sample, string gameName, string privacyUrl, bool childDirected)
        {
            if (string.IsNullOrEmpty(sample)) return null;
            string s = sample.Replace("\r\n", "\n");
            if (Count(s, SampleAtt) != 1 || Count(s, SampleUrl) != 1 || Count(s, SampleChild) != 1) return null;
            s = s.Replace(SampleAtt, AttBlock(gameName))
                .Replace(SampleUrl, "\"privacyPolicyUrl\": " + JsonString((privacyUrl ?? "").Trim()))
                .Replace(SampleChild, "\"childDirected\": " + (childDirected ? "true" : "false"))
                .Replace(SampleHelp, "\"_help\": \"Made by Studio Pulse's game setup (StudioAppSetup); stays KEYLESS in the repository (the release build writes the ids in: StudioAppRelease). ");
            return s;
        }

        /// <summary>
        /// An existing config with what is still empty filled in by text (its layout and comments kept):
        /// an empty privacyPolicyUrl, and the sample's empty English tracking prompt (en + tr then). Only
        /// patterns that occur exactly once are touched; the rest is left as it is.
        /// </summary>
        public static string FillMissing(string config, string gameName, string privacyUrl)
        {
            if (string.IsNullOrEmpty(config)) return config;
            string s = config.Replace("\r\n", "\n");
            if (!StudioAppConfig.Blank(privacyUrl) && Count(s, SampleUrl) == 1)
                s = s.Replace(SampleUrl, "\"privacyPolicyUrl\": " + JsonString(privacyUrl.Trim()));
            if (Count(s, SampleAtt) == 1)
                s = s.Replace(SampleAtt, AttBlock(gameName));
            return s;
        }

        static string AttBlock(string gameName)
        {
            return "\"attUsageDescription\": [\n"
                + "      { \"lang\": \"en\", \"text\": " + JsonString(AttText("en", gameName)) + " },\n"
                + "      { \"lang\": \"tr\", \"text\": " + JsonString(AttText("tr", gameName)) + " }\n    ]";
        }

        static int Count(string s, string part)
        {
            int n = 0;
            for (int i = s.IndexOf(part, StringComparison.Ordinal); i >= 0; i = s.IndexOf(part, i + part.Length, StringComparison.Ordinal)) n++;
            return n;
        }

        /// <summary>The define list with <paramref name="add"/> appended where missing (order kept, no blanks).</summary>
        public static string AddDefines(string existing, params string[] add)
        {
            var list = (existing ?? "").Split(';').Select(d => d.Trim()).Where(d => d.Length > 0).ToList();
            foreach (string d in add)
                if (!string.IsNullOrEmpty(d) && !list.Contains(d)) list.Add(d);
            return string.Join(";", list);
        }

        /// <summary>The keys set in a config that should stay keyless in a repository (empty: none).</summary>
        public static List<string> KeysIn(StudioAppConfig c)
        {
            var keys = new List<string>();
            if (c == null) return keys;
            if (c.pulse != null && !StudioAppConfig.Blank(c.pulse.writeKey)) keys.Add("pulse.writeKey");
            if (c.pulse != null && !StudioAppConfig.Blank(c.pulse.collectorUrl)) keys.Add("pulse.collectorUrl");
            if (c.levelplay != null && !StudioAppConfig.Blank(c.levelplay.iosAppKey)) keys.Add("levelplay.iosAppKey");
            if (c.levelplay != null && !StudioAppConfig.Blank(c.levelplay.androidAppKey)) keys.Add("levelplay.androidAppKey");
            if (c.max != null && !StudioAppConfig.Blank(c.max.sdkKey)) keys.Add("max.sdkKey");
            if (c.ump != null && !StudioAppConfig.Blank(c.ump.iosAppId)) keys.Add("ump.iosAppId");
            if (c.ump != null && !StudioAppConfig.Blank(c.ump.androidAppId)) keys.Add("ump.androidAppId");
            if (c.appsflyer != null && !StudioAppConfig.Blank(c.appsflyer.devKey)) keys.Add("appsflyer.devKey");
            return keys;
        }
    }
}
