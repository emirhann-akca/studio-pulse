// Pure C# (no UnityEngine): the game's studio-app.json (repository root, next to the Unity
// project) and the rules that turn it into the release build's Resources/studio_app.json.
// The Editor side (StudioAppRelease) reads the files with JsonUtility and calls these.
// docs/ADS_UNITY.md "Release keys" and docs/YENI_OYUN_KURULUM.md.
using System;
using System.IO;
using System.Text;

namespace Studio.Pulse
{
    /// <summary>
    /// The game's setup file, studio-app.json: what tools/ads_onboard.py and tools/new_game.py read
    /// and write (store ids, ad formats, the ad network ids) plus what the release build needs. The
    /// ids in it are public (they ship inside the app) but are kept out of the Unity project: the
    /// project's studio_app.json stays keyless, so development, test and simulator builds start
    /// nothing; the release build copies the ids in and restores the file byte for byte afterwards.
    /// The collector's write key is not in it either: it comes from the studio's local
    /// &lt;state&gt;/sdk/&lt;label&gt;.env (made by deploy/cloudrun/make_keys.py).
    /// Only the fields the Unity build uses are modelled here; the others are left alone.
    /// </summary>
    [Serializable]
    public sealed class StudioAppManifest
    {
        public const string FileName = "studio-app.json";
        /// <summary>Default: every non-development build gets the release keys.</summary>
        public const string InjectStore = "store";
        /// <summary>Only builds whose script calls StudioAppRelease.Apply / BuildPlayer get them.</summary>
        public const string InjectManual = "manual";

        public string name = "";
        /// <summary>The collector key's label (make_keys.py); empty: derived from <see cref="name"/>.</summary>
        public string pulse_label = "";
        /// <summary>The .env with the collector URL and write key; empty: &lt;state&gt;/sdk/&lt;label&gt;.env.</summary>
        public string pulse_env = "";
        /// <summary><see cref="InjectStore"/> (empty) or <see cref="InjectManual"/>.</summary>
        public string release_inject = "";
        /// <summary>The game's privacy policy (setup writes it into the keyless config).</summary>
        public string privacy_policy_url = "";
        /// <summary>Directed at children (setup writes privacy.childDirected).</summary>
        public bool coppa = false;
        public NetworksSection networks = new NetworksSection();

        [Serializable]
        public sealed class NetworksSection
        {
            public LevelPlayNetwork levelplay = new LevelPlayNetwork();
            public UmpNetwork ump = new UmpNetwork();
            public PulseNetwork pulse = new PulseNetwork();
        }

        [Serializable]
        public sealed class LevelPlayNetwork
        {
            public LevelPlayApp ios = new LevelPlayApp();
            public LevelPlayApp android = new LevelPlayApp();
        }

        [Serializable]
        public sealed class LevelPlayApp
        {
            public string app_key = "";
            public AdUnits ad_units = new AdUnits();
        }

        [Serializable]
        public sealed class AdUnits
        {
            public string rewarded = "";
            public string interstitial = "";
            public string banner = "";
        }

        [Serializable]
        public sealed class UmpNetwork
        {
            public string ios_app_id = "";
            public string android_app_id = "";
        }

        [Serializable]
        public sealed class PulseNetwork
        {
            public string collectorUrl = "";
            public string writeKey = "";
        }

        /// <summary>
        /// The key label: pulse_label, else the name in lower-case ASCII letters and digits
        /// ("Acorn Mart" -> acornmart, "Küp Dünyaları" -> kupdunyalari). make_keys.py's rule:
        /// a letter first, at most 32 characters. Empty when neither gives one.
        /// </summary>
        public string Label
        {
            get
            {
                string raw = !StudioAppConfig.Blank(pulse_label) ? pulse_label : name ?? "";
                var s = new StringBuilder();
                foreach (char c0 in raw.Trim())
                {
                    char c = Ascii(c0);
                    bool letter = c >= 'a' && c <= 'z';
                    bool tail = (c >= '0' && c <= '9') || c == '_';
                    if (letter || (tail && s.Length > 0)) s.Append(c);
                    if (s.Length == 32) break;
                }
                return s.ToString();
            }
        }

        static char Ascii(char c)
        {
            switch (c)
            {
                case 'ç': case 'Ç': return 'c';
                case 'ğ': case 'Ğ': return 'g';
                case 'ı': case 'I': case 'İ': case 'î': case 'Î': return 'i';
                case 'ö': case 'Ö': return 'o';
                case 'ş': case 'Ş': return 's';
                case 'ü': case 'Ü': case 'û': case 'Û': return 'u';
                case 'â': case 'Â': return 'a';
                default: return char.ToLowerInvariant(c);
            }
        }

        /// <summary>The release_inject mode (anything but "manual" is "store").</summary>
        public string InjectMode
        {
            get { return (release_inject ?? "").Trim().ToLowerInvariant() == InjectManual ? InjectManual : InjectStore; }
        }

        /// <summary>
        /// Writes the release ids into <paramref name="config"/> (and returns it). Ads (only when
        /// <paramref name="ads"/>): a platform's LevelPlay app key and ad units are replaced together
        /// when the manifest has any of them for that platform; the UMP (AdMob) app id per platform when
        /// set. Analytics (always): networks.pulse when set, else the collector URL and write key of
        /// <paramref name="pulseEnvText"/> (a make_keys.py .env; null: none). What the manifest does not
        /// give stays as the config has it. Nothing is read from disk.
        /// </summary>
        public StudioAppConfig ApplyTo(StudioAppConfig config, bool ads, string pulseEnvText)
        {
            StudioAppConfig c = config ?? new StudioAppConfig();
            NetworksSection n = networks ?? new NetworksSection();
            if (ads)
            {
                if (c.levelplay == null) c.levelplay = new StudioAppConfig.LevelPlaySection();
                LevelPlayNetwork lp = n.levelplay ?? new LevelPlayNetwork();
                if (Has(lp.ios))
                {
                    c.levelplay.iosAppKey = Clean(lp.ios.app_key);
                    c.levelplay.ios = Units(lp.ios.ad_units);
                }
                if (Has(lp.android))
                {
                    c.levelplay.androidAppKey = Clean(lp.android.app_key);
                    c.levelplay.android = Units(lp.android.ad_units);
                }
                if (c.ump == null) c.ump = new StudioAppConfig.UmpSection();
                UmpNetwork ump = n.ump ?? new UmpNetwork();
                if (!StudioAppConfig.Blank(ump.ios_app_id)) c.ump.iosAppId = Clean(ump.ios_app_id);
                if (!StudioAppConfig.Blank(ump.android_app_id)) c.ump.androidAppId = Clean(ump.android_app_id);
            }
            PulseNetwork pn = n.pulse ?? new PulseNetwork();
            string url = Clean(pn.collectorUrl), key = Clean(pn.writeKey);
            if (url.Length == 0 && key.Length == 0) PulseEnvFile.Parse(pulseEnvText, out url, out key);
            if (url.Length > 0 || key.Length > 0)
            {
                if (c.pulse == null) c.pulse = new StudioAppConfig.PulseSection();
                c.pulse.collectorUrl = url;
                c.pulse.writeKey = key;
            }
            return c;
        }

        static bool Has(LevelPlayApp a)
        {
            if (a == null) return false;
            if (!StudioAppConfig.Blank(a.app_key)) return true;
            AdUnits u = a.ad_units;
            return u != null && (!StudioAppConfig.Blank(u.rewarded) || !StudioAppConfig.Blank(u.interstitial) || !StudioAppConfig.Blank(u.banner));
        }

        static StudioAppConfig.AdUnitIds Units(AdUnits u)
        {
            u = u ?? new AdUnits();
            return new StudioAppConfig.AdUnitIds { rewarded = Clean(u.rewarded), interstitial = Clean(u.interstitial), banner = Clean(u.banner) };
        }

        static string Clean(string s)
        {
            return string.IsNullOrEmpty(s) ? "" : s.Trim();
        }
    }

    /// <summary>The .env files of deploy/cloudrun/make_keys.py (&lt;state&gt;/sdk/&lt;label&gt;.env).</summary>
    public static class PulseEnvFile
    {
        /// <summary>
        /// PULSE_COLLECTOR_URL and PULSE_WRITE_KEY from the text (blank lines, # comments, "export ",
        /// quotes and CRLF tolerated). True when both are set.
        /// </summary>
        public static bool Parse(string text, out string url, out string key)
        {
            url = "";
            key = "";
            if (string.IsNullOrEmpty(text)) return false;
            foreach (string raw in text.Split('\n'))
            {
                string line = raw.Trim();
                if (line.Length == 0 || line[0] == '#') continue;
                if (line.StartsWith("export ", StringComparison.Ordinal)) line = line.Substring(7).TrimStart();
                int eq = line.IndexOf('=');
                if (eq <= 0) continue;
                string name = line.Substring(0, eq).Trim();
                string value = line.Substring(eq + 1).Trim();
                if (value.Length >= 2 && (value[0] == '"' || value[0] == '\'') && value[value.Length - 1] == value[0])
                    value = value.Substring(1, value.Length - 2);
                if (name == "PULSE_COLLECTOR_URL") url = value.Trim();
                else if (name == "PULSE_WRITE_KEY") key = value.Trim();
            }
            return url.Length > 0 && key.Length > 0;
        }

        /// <summary>The host of a collector URL for logs (never the key); "invalid URL" otherwise.</summary>
        public static string Host(string url)
        {
            Uri u;
            return Uri.TryCreate((url ?? "").Trim(), UriKind.Absolute, out u) ? u.Host : "invalid URL";
        }
    }

    /// <summary>Where studio-pulse keeps its per-user files (src/studio_pulse/schedule.py user_state_dir).</summary>
    public static class PulseStudioPaths
    {
        public const string SdkEnvVariable = "STUDIO_PULSE_SDK_ENV";

        /// <summary>
        /// $PULSE_HOME, else per OS: Windows %LOCALAPPDATA%\studio-pulse, macOS ~/Library/Application
        /// Support/studio-pulse, others $XDG_STATE_HOME (or ~/.local/state)/studio-pulse.
        /// </summary>
        public static string StateDirectory(Func<string, string> env, string home, bool windows, bool mac)
        {
            string ph = env("PULSE_HOME");
            if (!string.IsNullOrEmpty(ph)) return ph;
            if (windows)
            {
                string local = env("LOCALAPPDATA");
                return Path.Combine(string.IsNullOrEmpty(local) ? Path.Combine(home, "AppData", "Local") : local, "studio-pulse");
            }
            if (mac) return Path.Combine(home, "Library", "Application Support", "studio-pulse");
            string xdg = env("XDG_STATE_HOME");
            return Path.Combine(string.IsNullOrEmpty(xdg) ? Path.Combine(home, ".local", "state") : xdg, "studio-pulse");
        }

        /// <summary>
        /// The collector .env of a game: $STUDIO_PULSE_SDK_ENV; else the manifest's pulse_env (relative
        /// to the manifest's folder, ~ is home); else &lt;state&gt;/sdk/&lt;label&gt;.env. Null without a label.
        /// </summary>
        public static string SdkEnvPath(StudioAppManifest manifest, string manifestDirectory, Func<string, string> env,
            string home, bool windows, bool mac)
        {
            string e = env(SdkEnvVariable);
            if (!string.IsNullOrEmpty(e)) return e;
            string own = manifest != null ? (manifest.pulse_env ?? "").Trim() : "";
            if (own.Length > 0)
            {
                if (own == "~" || own.StartsWith("~/", StringComparison.Ordinal)) own = Path.Combine(home, own.Length > 2 ? own.Substring(2) : "");
                return Path.IsPathRooted(own) ? own : Path.GetFullPath(Path.Combine(manifestDirectory ?? "", own));
            }
            string label = manifest != null ? manifest.Label : "";
            if (label.Length == 0) return null;
            return Path.Combine(StateDirectory(env, home, windows, mac), "sdk", label + ".env");
        }
    }
}
