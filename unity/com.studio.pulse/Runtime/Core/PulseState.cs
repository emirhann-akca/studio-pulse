// Pure C# (no UnityEngine): the per-install state file and a small JSON reader.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

namespace Studio.Pulse
{
    /// <summary>
    /// Persisted per install in DataDirectory/state.json (atomic replace). Written on
    /// lifecycle changes and before queue files are deleted, never per event: the
    /// sequence number of queued events is recovered from the queue itself.
    /// </summary>
    internal sealed class PulseState
    {
        public string InstallId;
        public long Seq;              // highest seq known to be assigned
        public int Snum;
        public string Sid;
        public long SessionStartMs;   // wall clock
        public long LastActiveMs;     // wall clock of the last background / quit / write
        public string AppVersion;     // version of the last launch
        public bool Foreground;       // the process was in the foreground at the last write
        public bool CleanExit;        // the last process quit through OnQuit()
        public bool FatalError;       // the last process recorded a fatal error
        public long LaunchMs;         // wall clock when the last process started
        public string Consent;        // granted / denied / null
        public bool OptedOut;
        public long Dropped;
        public readonly Dictionary<long, int> Attempts = new Dictionary<long, int>();
        public readonly List<string> PendingDeletes = new List<string>();

        public string ToJson()
        {
            var w = new PulseJsonWriter(512);
            w.Byte((byte)'{');
            bool c = false;
            if (OptedOut) { w.Key("opted_out", c); w.Bool(true); c = true; }
            if (InstallId != null) { w.Key("install_id", c); w.String(InstallId); c = true; }
            w.Key("seq", c); w.Long(Seq); c = true;
            w.Key("snum", c); w.Long(Snum);
            if (Sid != null) { w.Key("sid", c); w.String(Sid); }
            w.Key("session_start_ms", c); w.Long(SessionStartMs);
            w.Key("last_active_ms", c); w.Long(LastActiveMs);
            if (AppVersion != null) { w.Key("app_version", c); w.String(AppVersion); }
            w.Key("foreground", c); w.Bool(Foreground);
            w.Key("clean_exit", c); w.Bool(CleanExit);
            w.Key("fatal_error", c); w.Bool(FatalError);
            w.Key("launch_ms", c); w.Long(LaunchMs);
            if (Consent != null) { w.Key("consent", c); w.String(Consent); }
            w.Key("dropped", c); w.Long(Dropped);
            w.Key("attempts", c);
            w.Byte((byte)'{');
            bool first = true;
            foreach (var kv in Attempts)
            {
                w.Key(kv.Key.ToString(CultureInfo.InvariantCulture), !first);
                w.Long(kv.Value);
                first = false;
            }
            w.Byte((byte)'}');
            w.Key("pending_deletes", c);
            w.Byte((byte)'[');
            for (int i = 0; i < PendingDeletes.Count; i++)
            {
                if (i > 0) w.Byte((byte)',');
                w.String(PendingDeletes[i]);
            }
            w.Byte((byte)']');
            w.Byte((byte)'}');
            return Encoding.UTF8.GetString(w.Buffer, 0, w.Length);
        }

        public static PulseState FromJson(string json)
        {
            var o = MiniJson.Parse(json) as Dictionary<string, object>;
            if (o == null) return null;
            var s = new PulseState
            {
                OptedOut = MiniJson.Bool(o, "opted_out"),
                InstallId = MiniJson.Str(o, "install_id"),
                Seq = MiniJson.Long(o, "seq"),
                Snum = (int)MiniJson.Long(o, "snum"),
                Sid = MiniJson.Str(o, "sid"),
                SessionStartMs = MiniJson.Long(o, "session_start_ms"),
                LastActiveMs = MiniJson.Long(o, "last_active_ms"),
                AppVersion = MiniJson.Str(o, "app_version"),
                Foreground = MiniJson.Bool(o, "foreground"),
                CleanExit = MiniJson.Bool(o, "clean_exit"),
                FatalError = MiniJson.Bool(o, "fatal_error"),
                LaunchMs = MiniJson.Long(o, "launch_ms"),
                Consent = MiniJson.Str(o, "consent"),
                Dropped = MiniJson.Long(o, "dropped"),
            };
            if (o.TryGetValue("attempts", out object a) && a is Dictionary<string, object> am)
            {
                foreach (var kv in am)
                {
                    if (long.TryParse(kv.Key, NumberStyles.Integer, CultureInfo.InvariantCulture, out long lvl) && kv.Value is long n)
                    {
                        s.Attempts[lvl] = (int)n;
                    }
                }
            }
            if (o.TryGetValue("pending_deletes", out object d) && d is List<object> dl)
            {
                foreach (object x in dl)
                {
                    if (x is string id) s.PendingDeletes.Add(id);
                }
            }
            return s;
        }

        public static PulseState Load(string path)
        {
            try
            {
                if (!File.Exists(path)) path += ".tmp";
                if (!File.Exists(path)) return null;
                return FromJson(File.ReadAllText(path, Encoding.UTF8));
            }
            catch (Exception)
            {
                return null; // a corrupt state file starts a new install
            }
        }

        public void Save(string path)
        {
            string tmp = path + ".tmp";
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            byte[] data = Encoding.UTF8.GetBytes(ToJson());
            using (var fs = new FileStream(tmp, FileMode.Create, FileAccess.Write, FileShare.None))
            {
                fs.Write(data, 0, data.Length);
                fs.Flush(true);
            }
            if (!File.Exists(path)) { File.Move(tmp, path); return; }
            try
            {
                File.Replace(tmp, path, null);
            }
            catch (Exception)
            {
                // Some file systems lack an atomic replace: Load() falls back to the .tmp
                // copy if the process dies between these two calls.
                File.Delete(path);
                File.Move(tmp, path);
            }
        }
    }

    /// <summary>Just enough JSON reading for the state file and collector answers.</summary>
    public static class MiniJson
    {
        public static object Parse(string s)
        {
            int i = 0;
            object v = Value(s, ref i);
            Ws(s, ref i);
            if (i != s.Length) throw new FormatException("trailing characters");
            return v;
        }

        static void Ws(string s, ref int i)
        {
            while (i < s.Length && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) i++;
        }

        static object Value(string s, ref int i)
        {
            Ws(s, ref i);
            if (i >= s.Length) throw new FormatException("unexpected end");
            char c = s[i];
            if (c == '{')
            {
                var d = new Dictionary<string, object>();
                i++;
                Ws(s, ref i);
                if (s[i] == '}') { i++; return d; }
                while (true)
                {
                    Ws(s, ref i);
                    string k = Str(s, ref i);
                    Ws(s, ref i);
                    if (s[i++] != ':') throw new FormatException("expected :");
                    d[k] = Value(s, ref i);
                    Ws(s, ref i);
                    char e = s[i++];
                    if (e == '}') return d;
                    if (e != ',') throw new FormatException("expected , or }");
                }
            }
            if (c == '[')
            {
                var l = new List<object>();
                i++;
                Ws(s, ref i);
                if (s[i] == ']') { i++; return l; }
                while (true)
                {
                    l.Add(Value(s, ref i));
                    Ws(s, ref i);
                    char e = s[i++];
                    if (e == ']') return l;
                    if (e != ',') throw new FormatException("expected , or ]");
                }
            }
            if (c == '"') return Str(s, ref i);
            if (s.Length - i >= 4 && string.CompareOrdinal(s, i, "true", 0, 4) == 0) { i += 4; return true; }
            if (s.Length - i >= 5 && string.CompareOrdinal(s, i, "false", 0, 5) == 0) { i += 5; return false; }
            if (s.Length - i >= 4 && string.CompareOrdinal(s, i, "null", 0, 4) == 0) { i += 4; return null; }
            int start = i;
            bool frac = false;
            while (i < s.Length && "+-0123456789.eE".IndexOf(s[i]) >= 0)
            {
                if (s[i] == '.' || s[i] == 'e' || s[i] == 'E') frac = true;
                i++;
            }
            if (i == start) throw new FormatException("unexpected character");
            string num = s.Substring(start, i - start);
            if (!frac && long.TryParse(num, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out long lv)) return lv;
            return double.Parse(num, NumberStyles.Float, CultureInfo.InvariantCulture);
        }

        static string Str(string s, ref int i)
        {
            if (s[i] != '"') throw new FormatException("expected string");
            i++;
            var sb = new StringBuilder();
            while (true)
            {
                char c = s[i++];
                if (c == '"') return sb.ToString();
                if (c != '\\') { sb.Append(c); continue; }
                char e = s[i++];
                switch (e)
                {
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'u':
                        sb.Append((char)int.Parse(s.Substring(i, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture));
                        i += 4;
                        break;
                    default: sb.Append(e); break;
                }
            }
        }

        public static string Str(Dictionary<string, object> o, string k)
        {
            return o.TryGetValue(k, out object v) ? v as string : null;
        }

        public static long Long(Dictionary<string, object> o, string k)
        {
            if (!o.TryGetValue(k, out object v)) return 0;
            if (v is long l) return l;
            if (v is double d) return (long)d;
            return 0;
        }

        public static bool Bool(Dictionary<string, object> o, string k)
        {
            return o.TryGetValue(k, out object v) && v is bool b && b;
        }
    }
}
