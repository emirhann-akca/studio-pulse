using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace Studio.Pulse.Tests
{
    /// <summary>
    /// Replays contract/golden/sdk_script.json; the request bodies must equal
    /// contract/golden/sdk_conformance.ndjson byte for byte (the Dart SDK runs the same
    /// script against the same file). PULSE_WRITE_GOLDEN=1 rewrites the golden file.
    /// </summary>
    public static class ConformanceTests
    {
        static string S(Dictionary<string, object> a, string k) { return a != null && a.TryGetValue(k, out object v) ? v as string : null; }
        static long L(Dictionary<string, object> a, string k) { return Convert.ToInt64(a[k]); }
        static long? LN(Dictionary<string, object> a, string k) { return a != null && a.TryGetValue(k, out object v) && v != null ? Convert.ToInt64(v) : (long?)null; }
        static double D(Dictionary<string, object> a, string k) { return Convert.ToDouble(a[k]); }
        static double? DN(Dictionary<string, object> a, string k) { return a != null && a.TryGetValue(k, out object v) && v != null ? Convert.ToDouble(v) : (double?)null; }
        static bool? BN(Dictionary<string, object> a, string k) { return a != null && a.TryGetValue(k, out object v) && v is bool b ? b : (bool?)null; }

        public static PulseParams Params(List<object> list)
        {
            var p = new PulseParams();
            foreach (List<object> t in list)
            {
                string key = (string)t[0];
                switch ((string)t[1])
                {
                    case "i": p.Add(key, Convert.ToInt64(t[2])); break;
                    case "d": p.Add(key, Convert.ToDouble(t[2])); break;
                    case "b": p.Add(key, (bool)t[2]); break;
                    default: p.Add(key, (string)t[2]); break;
                }
            }
            return p;
        }

        public static PulseDevice Device(Dictionary<string, object> d)
        {
            return new PulseDevice
            {
                AppId = S(d, "app_id"),
                Platform = S(d, "platform"),
                AppVersion = S(d, "app_version"),
                Build = S(d, "build"),
                BuildEnv = S(d, "build_env"),
                InstallStore = S(d, "install_store"),
                InstallerRaw = S(d, "installer_raw"),
                AppName = S(d, "app_name"),
                Sdk = S(d, "sdk"),
                Engine = S(d, "engine"),
                Os = S(d, "os"),
                DeviceModel = S(d, "device_model"),
                DeviceTier = S(d, "device_tier"),
                RamMb = (int)L(d, "ram_mb"),
                Locale = S(d, "locale"),
                TzOffsetMin = (int)L(d, "tz_offset_min"),
            };
        }

        /// <summary>Runs the script; returns the uncompressed request bodies.</summary>
        public static List<string> Replay(string scriptPath)
        {
            var script = (Dictionary<string, object>)MiniJson.Parse(File.ReadAllText(scriptPath, Encoding.UTF8));
            long start = L(script, "start_ms");
            var clock = new FakeClock(start);
            var rnd = new SeededRandom((ulong)L(script, "seed"));
            var transport = new FakeTransport();
            var device = Device((Dictionary<string, object>)script["device"]);
            string dir = Path.Combine(Path.GetTempPath(), "pulse-conf-" + Guid.NewGuid().ToString("N"));
            PulseCore core = null;
            PulseCore NewCore()
            {
                var env = new PulseEnvironment { DataDirectory = dir, Clock = clock, Random = rnd, Transport = transport };
                var c = new PulseCore(S(script, "write_key"), S(script, "endpoint"), new PulseOptions(), device, env);
                c.Start();
                return c;
            }
            try
            {
                foreach (Dictionary<string, object> step in (List<object>)script["steps"])
                {
                    long at = start + L(step, "at");
                    if (at > clock.Now) clock.Advance(at - clock.Now);
                    if (core != null) core.Tick();
                    var a = step.TryGetValue("args", out object ao) ? (Dictionary<string, object>)ao : null;
                    switch ((string)step["call"])
                    {
                        case "start": core = NewCore(); break;
                        case "restart":
                            device.AppVersion = S(a, "app_version") ?? device.AppVersion;
                            device.Build = S(a, "build") ?? device.Build;
                            core = NewCore();
                            break;
                        case "tick": break;
                        case "flush": core.Flush(); break;
                        case "background": core.OnBackground(); break;
                        case "foreground": core.OnForeground(); break;
                        case "set_consent": core.SetConsent((bool)a["granted"]); break;
                        case "set_user_id": core.SetUserId(S(a, "uid")); break;
                        case "tutorial_begin": core.TutorialBegin(S(a, "tutorial")); break;
                        case "tutorial_step": core.TutorialStep(L(a, "step"), S(a, "step_name"), S(a, "tutorial")); break;
                        case "tutorial_complete": core.TutorialComplete(S(a, "tutorial")); break;
                        case "level_start": core.LevelStart(L(a, "level"), S(a, "level_name"), S(a, "world"), S(a, "mode"), (int)(LN(a, "attempt") ?? 0)); break;
                        case "level_complete": core.LevelComplete(L(a, "level"), LN(a, "score"), (int?)LN(a, "stars"), LN(a, "moves_left")); break;
                        case "level_fail": core.LevelFail(L(a, "level"), S(a, "reason"), DN(a, "pos_x"), DN(a, "pos_y"), DN(a, "progress"), S(a, "pos_space"), LN(a, "score")); break;
                        case "level_quit": core.LevelQuit(L(a, "level"), S(a, "reason"), DN(a, "progress"), DN(a, "pos_x"), DN(a, "pos_y"), S(a, "pos_space")); break;
                        case "setting_change": core.SettingChange(S(a, "setting"), S(a, "from"), S(a, "to")); break;
                        case "earn_currency": core.EarnCurrency(S(a, "name"), D(a, "value"), S(a, "item"), LN(a, "level")); break;
                        case "spend_currency": core.SpendCurrency(S(a, "name"), D(a, "value"), S(a, "item"), LN(a, "level")); break;
                        case "purchase": core.Purchase(S(a, "transaction_id"), S(a, "product_id"), L(a, "price_micros"), S(a, "currency"), BN(a, "is_restore"), BN(a, "is_sandbox")); break;
                        case "ad_impression": core.AdImpression(S(a, "ad_platform"), S(a, "ad_format"), D(a, "value"), S(a, "currency"), S(a, "ad_source"), S(a, "ad_unit_name"), S(a, "placement"), S(a, "precision")); break;
                        case "ad_event": core.AdEvent(S(a, "action"), S(a, "ad_format"), S(a, "placement"), S(a, "error_code")); break;
                        case "screen": core.Screen(S(a, "name"), S(a, "class")); break;
                        case "track": core.Track(S(a, "name"), Params((List<object>)a["params"])); break;
                        case "perf": core.RecordPerf(S(a, "name"), Params((List<object>)a["params"])); break;
                        case "error": core.ReportError(S(a, "type"), S(a, "message"), S(a, "stack"), BN(a, "fatal") ?? false); break;
                        default: throw new Exception("unknown call " + step["call"]);
                    }
                }
                core.Close();
                return transport.Requests.Select(r => r.Json).ToList();
            }
            finally
            {
                try { Directory.Delete(dir, true); } catch (Exception) { }
            }
        }

        [PulseTest]
        public static void Batches_match_the_cross_sdk_golden_byte_for_byte()
        {
            string script = PulseTestRunner.FindRepoFile("contract/golden/sdk_script.json");
            string goldenPath = Path.Combine(Path.GetDirectoryName(script), "sdk_conformance.ndjson");
            List<string> bodies = Replay(script);
            string text = string.Join("\n", bodies) + "\n";
            if (Environment.GetEnvironmentVariable("PULSE_WRITE_GOLDEN") == "1")
            {
                File.WriteAllBytes(goldenPath, new UTF8Encoding(false).GetBytes(text));
                throw new SkipException("golden written: " + goldenPath);
            }
            string golden = File.ReadAllText(goldenPath, Encoding.UTF8).Replace("\r\n", "\n");
            var want = golden.Split('\n').Where(l => l.Length > 0).ToList();
            Check.Equal(want.Count, bodies.Count, "number of requests");
            for (int i = 0; i < want.Count; i++)
            {
                if (want[i] != bodies[i])
                {
                    int at = 0;
                    while (at < want[i].Length && at < bodies[i].Length && want[i][at] == bodies[i][at]) at++;
                    int from = Math.Max(0, at - 60);
                    throw new Exception("request " + (i + 1) + " differs at char " + at + ":\n want ..." + Snip(want[i], from) + "\n got  ..." + Snip(bodies[i], from));
                }
            }
            // the second run with the same seed is identical too (no hidden state, no real clock)
            Check.True(Replay(script).SequenceEqual(bodies), "deterministic");
        }

        static string Snip(string s, int from)
        {
            return s.Substring(from, Math.Min(160, s.Length - from));
        }
    }
}
