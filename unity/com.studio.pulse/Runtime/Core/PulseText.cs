// Pure C# (no UnityEngine): error scrubbing and fingerprints, install store
// mapping. The Dart SDK implements the same rules (conformance-tested).
using System;
using System.Text;

namespace Studio.Pulse
{
    public static class PulseErrors
    {
        public const int MsgMaxLen = 256;
        public const int FrameMaxLen = 100;
        public const int TypeMaxLen = 100;

        /// <summary>
        /// First line of the message with anything that can identify a person or a device
        /// replaced: tokens with '@' -> &lt;email&gt;, with '/' or '\' -> &lt;path&gt;, 0x... -> &lt;hex&gt;,
        /// long hex/uuid tokens -> &lt;id&gt;, digit runs -> #. At most 256 characters.
        /// </summary>
        public static string Scrub(string message)
        {
            if (string.IsNullOrEmpty(message)) return "";
            int nl = message.IndexOf('\n');
            string line = (nl >= 0 ? message.Substring(0, nl) : message).Trim();
            string[] tokens = line.Split(' ');
            var sb = new StringBuilder(line.Length);
            for (int t = 0; t < tokens.Length; t++)
            {
                if (t > 0) sb.Append(' ');
                string tok = tokens[t];
                if (tok.IndexOf('@') >= 0) sb.Append("<email>");
                else if (tok.IndexOf('/') >= 0 || tok.IndexOf('\\') >= 0) sb.Append("<path>");
                else if (tok.Length > 2 && tok[0] == '0' && (tok[1] == 'x' || tok[1] == 'X')) sb.Append("<hex>");
                else if (IsIdLike(tok)) sb.Append("<id>");
                else
                {
                    bool inDigits = false;
                    for (int i = 0; i < tok.Length; i++)
                    {
                        char c = tok[i];
                        if (c >= '0' && c <= '9')
                        {
                            if (!inDigits) sb.Append('#');
                            inDigits = true;
                        }
                        else
                        {
                            inDigits = false;
                            sb.Append(c);
                        }
                    }
                }
            }
            string s = sb.ToString();
            return s.Substring(0, PulseJsonWriter.CutLength(s, MsgMaxLen));
        }

        static bool IsIdLike(string tok)
        {
            if (tok.Length < 8) return false;
            bool digit = false;
            for (int i = 0; i < tok.Length; i++)
            {
                char c = tok[i];
                bool isDigit = c >= '0' && c <= '9';
                bool hex = isDigit || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
                if (!hex && c != '-') return false;
                digit |= isDigit;
            }
            return digit;
        }

        /// <summary>The first stack frame as "Type.Method" (Unity, .NET, IL2CPP and Dart formats).</summary>
        public static string TopFrame(string stack)
        {
            if (string.IsNullOrEmpty(stack)) return null;
            string[] lines = stack.Split('\n');
            for (int i = 0; i < lines.Length; i++)
            {
                string l = lines[i].Trim();
                if (l.Length == 0) continue;
                if (l[0] == '#')
                {
                    int j = 1;
                    while (j < l.Length && l[j] >= '0' && l[j] <= '9') j++;
                    l = l.Substring(j).Trim();
                }
                if (l.StartsWith("at ", StringComparison.Ordinal)) l = l.Substring(3).Trim();
                int p = l.IndexOf('(');
                if (p >= 0) l = l.Substring(0, p).Trim();
                if (l.Length == 0) continue;
                return l.Substring(0, PulseJsonWriter.CutLength(l, FrameMaxLen));
            }
            return null;
        }

        /// <summary>FNV-1a 64 of UTF-8 "type\nframe\nscrubbed message", 16 lowercase hex digits.</summary>
        public static string Fingerprint(string type, string frame, string scrubbed)
        {
            byte[] data = Encoding.UTF8.GetBytes((type ?? "") + "\n" + (frame ?? "") + "\n" + (scrubbed ?? ""));
            ulong h = 14695981039346656037UL;
            unchecked
            {
                for (int i = 0; i < data.Length; i++)
                {
                    h ^= data[i];
                    h *= 1099511628211UL;
                }
            }
            return h.ToString("x16");
        }
    }

    public static class PulseStores
    {
        /// <summary>Android installer package (InstallSourceInfo.getInstallingPackageName / getInstallerPackageName) -> install_store.</summary>
        public static string FromAndroidInstaller(string installer)
        {
            if (string.IsNullOrEmpty(installer)) return PulseContract.InstallStore.Sideload;
            switch (installer)
            {
                case "com.android.vending":
                case "com.google.android.feedback":
                    return PulseContract.InstallStore.GooglePlay;
                case "com.amazon.venezia":
                    return PulseContract.InstallStore.Amazon;
                case "com.sec.android.app.samsungapps":
                    return PulseContract.InstallStore.Samsung;
                case "com.huawei.appmarket":
                    return PulseContract.InstallStore.Huawei;
                case "com.xiaomi.market":
                case "com.xiaomi.mipicks":
                case "com.xiaomi.discover":
                    return PulseContract.InstallStore.Xiaomi;
                case "com.google.android.packageinstaller":
                case "com.android.packageinstaller":
                case "com.android.shell":
                    return PulseContract.InstallStore.Sideload;
                default:
                    return PulseContract.InstallStore.Unknown;
            }
        }

        /// <summary>The iOS plug-in's answer (AppTransaction.environment, else the receipt) -> install_store.</summary>
        public static string FromIos(string native)
        {
            switch (native)
            {
                case "production": case "app_store": return PulseContract.InstallStore.AppStore;
                case "sandbox": case "testflight": return PulseContract.InstallStore.Testflight;
                case "xcode": case "sideload": return PulseContract.InstallStore.Sideload;
                case "simulator": return PulseContract.InstallStore.Simulator;
                default: return PulseContract.InstallStore.Unknown;
            }
        }
    }
}
