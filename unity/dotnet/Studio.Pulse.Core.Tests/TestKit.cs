// Test helpers for the Unity SDK core. No test framework dependency: the same
// [PulseTest] methods run under Mono with MonoMain.cs (this Mac, no dotnet) and
// under `dotnet test` through XunitAdapter.cs (CI on Windows, macOS, Linux).
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Text;

namespace Studio.Pulse.Tests
{
    [AttributeUsage(AttributeTargets.Method)]
    public sealed class PulseTestAttribute : Attribute
    {
    }

    public sealed class SkipException : Exception
    {
        public SkipException(string why) : base(why) { }
    }

    public static class Check
    {
        public static void True(bool cond, string what)
        {
            if (!cond) throw new Exception("check failed: " + what);
        }

        public static void Equal<T>(T expected, T actual, string what)
        {
            if (!EqualityComparer<T>.Default.Equals(expected, actual))
            {
                throw new Exception("check failed: " + what + ": expected <" + expected + "> got <" + actual + ">");
            }
        }

        public static void InRange(long v, long lo, long hi, string what)
        {
            if (v < lo || v > hi) throw new Exception("check failed: " + what + ": " + v + " not in [" + lo + ", " + hi + "]");
        }
    }

    public sealed class FakeClock : IPulseClock
    {
        public long Now;
        public long Mono = 1_000_000;

        public FakeClock(long startMs) { Now = startMs; }

        public long NowMs() { return Now; }
        public long MonotonicMs() { return Mono; }

        public void Advance(long ms)
        {
            Now += ms;
            Mono += ms;
        }
    }

    /// <summary>One captured request.</summary>
    public sealed class Sent
    {
        public string Url;
        public bool Gzip;
        public byte[] Raw;
        public string Json;     // the uncompressed body
        public Action<PulseHttpResult> Done;
        public Dictionary<string, object> Body => MiniJson.Parse(Json) as Dictionary<string, object>;
        public List<object> Events => (List<object>)Body["events"];
    }

    /// <summary>Records requests; answers through <see cref="Responder"/> (null: leave them pending).</summary>
    public sealed class FakeTransport : IPulseTransport
    {
        public readonly List<Sent> Requests = new List<Sent>();
        public Func<Sent, PulseHttpResult?> Responder = s => Ok(s);
        public FakeCollector Collector;

        public static PulseHttpResult Status(int status, int retryAfter = -1, string body = null)
        {
            return new PulseHttpResult { Status = status, RetryAfterSeconds = retryAfter, Body = body };
        }

        public static PulseHttpResult? Ok(Sent s)
        {
            return Status(s.Url.EndsWith("/v2/delete") ? 202 : 200, -1, "{\"ok\":true}");
        }

        public void Post(string url, string writeKey, byte[] body, int length, bool gzip, Action<PulseHttpResult> done)
        {
            var raw = new byte[length];
            Buffer.BlockCopy(body, 0, raw, 0, length);
            var s = new Sent { Url = url, Gzip = gzip, Raw = raw, Done = done, Json = Encoding.UTF8.GetString(gzip ? Gunzip(raw) : raw) };
            Requests.Add(s);
            PulseHttpResult? r = Responder(s);
            if (r.HasValue)
            {
                if (r.Value.Status >= 200 && r.Value.Status < 300 && Collector != null && url.EndsWith("/v2/batch")) Collector.Receive(s);
                done(r.Value);
            }
        }

        public static byte[] Gunzip(byte[] data)
        {
            using (var src = new MemoryStream(data))
            using (var gz = new GZipStream(src, CompressionMode.Decompress))
            using (var dst = new MemoryStream())
            {
                gz.CopyTo(dst);
                return dst.ToArray();
            }
        }

        public List<Sent> Batches => Requests.Where(r => r.Url.EndsWith("/v2/batch")).ToList();
    }

    /// <summary>The collector's dedupe in miniature: one row per event id and per (install_id, seq).</summary>
    public sealed class FakeCollector
    {
        public readonly List<Dictionary<string, object>> Rows = new List<Dictionary<string, object>>();
        public readonly HashSet<string> Ids = new HashSet<string>();
        public readonly HashSet<string> Keys = new HashSet<string>();
        public int Duplicates;

        public void Receive(Sent s)
        {
            var body = s.Body;
            var ctx = (Dictionary<string, object>)body["ctx"];
            string install = (string)ctx["install_id"];
            foreach (Dictionary<string, object> ev in (List<object>)body["events"])
            {
                string id = (string)ev["id"];
                string key = install + "/" + ev["seq"];
                if (Ids.Contains(id) || Keys.Contains(key)) { Duplicates++; continue; }
                Ids.Add(id);
                Keys.Add(key);
                var row = new Dictionary<string, object>(ev) { ["install_id"] = install, ["app_version"] = ctx["app_version"] };
                Rows.Add(row);
            }
        }

        public List<long> Seqs(string install = null)
        {
            return Rows.Where(r => install == null || (string)r["install_id"] == install).Select(r => (long)r["seq"]).OrderBy(x => x).ToList();
        }

        public List<string> Names() { return Rows.Select(r => (string)r["name"]).ToList(); }
    }

    /// <summary>A device + data directory + core, restartable on the same directory.</summary>
    public sealed class Rig : IDisposable
    {
        public readonly string Dir;
        public readonly FakeClock Clock;
        public readonly SeededRandom Random;
        public readonly FakeTransport Transport = new FakeTransport();
        public readonly FakeCollector Collector = new FakeCollector();
        public readonly List<string> Log = new List<string>();
        public PulseOptions Options = new PulseOptions();
        public PulseDevice Device;
        public string NoBackupDir;
        public Func<PulseExitInfo> PreviousExit;
        public PulseCore Core;

        public Rig(ulong seed = 7, long startMs = 1794499900000L)
        {
            Dir = Path.Combine(Path.GetTempPath(), "pulse-test-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Dir);
            Clock = new FakeClock(startMs);
            Random = new SeededRandom(seed);
            Transport.Collector = Collector;
            Device = DefaultDevice();
        }

        public static PulseDevice DefaultDevice()
        {
            return new PulseDevice
            {
                AppId = "com.example.puzzle",
                Platform = "android",
                AppVersion = "1.4.0",
                Build = "140",
                BuildEnv = "release",
                InstallStore = "google_play",
                InstallerRaw = "com.android.vending",
                AppName = "Example Puzzle",
                Sdk = "unity/" + PulseCore.Version,
                Engine = "unity 6000.3",
                Os = "android 14",
                DeviceModel = "SM-A145F",
                DeviceTier = "low",
                RamMb = 3072,
                Locale = "tr-TR",
                TzOffsetMin = 180,
            };
        }

        public PulseCore Start()
        {
            var env = new PulseEnvironment
            {
                DataDirectory = Path.Combine(Dir, "data"),
                NoBackupDirectory = NoBackupDir,
                PreviousExit = PreviousExit,
                Clock = Clock,
                Random = Random,
                Transport = Transport,
                Log = m => Log.Add(m),
            };
            Core = new PulseCore("sp_pub_local", "http://127.0.0.1:8787", Options, Device, env);
            Core.Start();
            return Core;
        }

        /// <summary>
        /// Simulates a process kill: the old core is abandoned (no quit, no flush, no answer
        /// handled). Only its open queue file is closed, as the OS does for a dead process;
        /// Windows would otherwise refuse to let the next launch read a file another handle
        /// is writing.
        /// </summary>
        public PulseCore Restart()
        {
            try { Core?.Close(); } catch (Exception) { }
            return Start();
        }

        public void Step(long ms, int ticks = 1)
        {
            for (int i = 0; i < ticks; i++)
            {
                Clock.Advance(ms);
                Core.Tick();
            }
        }

        /// <summary>A few short ticks: answers are applied and the next batches go out.</summary>
        public void Drain(int ticks = 12)
        {
            Step(10, ticks);
        }

        public string DataDir => Path.Combine(Dir, "data");

        public string[] QueueFiles()
        {
            string q = Path.Combine(DataDir, "queue");
            return Directory.Exists(q) ? Directory.GetFiles(q) : new string[0];
        }

        public void Dispose()
        {
            try { Core?.Close(); } catch (Exception) { }
            try { Directory.Delete(Dir, true); } catch (Exception) { }
        }
    }

    public static class PulseTestRunner
    {
        public static List<MethodInfo> Discover()
        {
            return typeof(PulseTestRunner).Assembly.GetTypes()
                .SelectMany(t => t.GetMethods(BindingFlags.Public | BindingFlags.Static))
                .Where(m => m.GetCustomAttribute<PulseTestAttribute>() != null)
                .OrderBy(m => m.DeclaringType.Name + "." + m.Name, StringComparer.Ordinal)
                .ToList();
        }

        public static string NameOf(MethodInfo m) { return m.DeclaringType.Name + "." + m.Name; }

        /// <summary>null = passed; "SKIP: ..." = skipped; anything else = the failure.</summary>
        public static string Run(MethodInfo m)
        {
            try
            {
                m.Invoke(null, null);
                return null;
            }
            catch (TargetInvocationException ex) when (ex.InnerException is SkipException)
            {
                return "SKIP: " + ex.InnerException.Message;
            }
            catch (TargetInvocationException ex)
            {
                return ex.InnerException.ToString();
            }
        }

        public static string FindRepoFile(string rel)
        {
            string dir = AppContext.BaseDirectory;
            for (int i = 0; i < 10 && dir != null; i++)
            {
                string p = Path.Combine(dir, rel);
                if (File.Exists(p)) return p;
                dir = Path.GetDirectoryName(dir.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
            }
            string env = Environment.GetEnvironmentVariable("PULSE_REPO");
            if (env != null && File.Exists(Path.Combine(env, rel))) return Path.Combine(env, rel);
            throw new FileNotFoundException(rel + " not found above " + AppContext.BaseDirectory + " (set PULSE_REPO)");
        }
    }
}
