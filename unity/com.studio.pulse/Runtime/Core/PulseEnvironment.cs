// Pure C# (no UnityEngine): everything the SDK core needs from the outside world,
// behind small interfaces so the core runs the same in Unity, in a console app and
// in tests with a fake clock, a seeded random source and a fake transport.
using System;
using System.Diagnostics;
using System.Security.Cryptography;

namespace Studio.Pulse
{
    public interface IPulseClock
    {
        /// <summary>Device wall clock, ms since the Unix epoch (may jump; only used for timestamps).</summary>
        long NowMs();

        /// <summary>Monotonic ms (any origin); durations, timers and foreground time use it.</summary>
        long MonotonicMs();
    }

    public sealed class SystemClock : IPulseClock
    {
        readonly Stopwatch _sw = Stopwatch.StartNew();

        public long NowMs()
        {
            return DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        }

        public long MonotonicMs()
        {
            return _sw.ElapsedMilliseconds;
        }
    }

    public interface IPulseRandom
    {
        void NextBytes(byte[] buffer, int offset, int count);
    }

    /// <summary>Cryptographic random bytes (install ids, session ids, event ids, jitter).</summary>
    public sealed class CryptoRandom : IPulseRandom
    {
        readonly RandomNumberGenerator _rng = RandomNumberGenerator.Create();
        readonly byte[] _tmp = new byte[16];

        public void NextBytes(byte[] buffer, int offset, int count)
        {
            lock (_tmp)
            {
                while (count > 0)
                {
                    int n = Math.Min(count, _tmp.Length);
                    _rng.GetBytes(_tmp, 0, n);
                    System.Buffer.BlockCopy(_tmp, 0, buffer, offset, n);
                    offset += n;
                    count -= n;
                }
            }
        }
    }

    /// <summary>
    /// SplitMix64, byte for byte the same in the Dart SDK: the conformance test runs
    /// both SDKs with one seed and expects identical ids. Not for production ids.
    /// </summary>
    public sealed class SeededRandom : IPulseRandom
    {
        ulong _state;

        public SeededRandom(ulong seed)
        {
            _state = seed;
        }

        public ulong NextUInt64()
        {
            unchecked
            {
                _state += 0x9E3779B97F4A7C15UL;
                ulong z = _state;
                z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9UL;
                z = (z ^ (z >> 27)) * 0x94D049BB133111EBUL;
                return z ^ (z >> 31);
            }
        }

        /// <summary>Each 8 bytes come from one draw, low byte first; a partial tail uses one more draw.</summary>
        public void NextBytes(byte[] buffer, int offset, int count)
        {
            while (count > 0)
            {
                ulong v = NextUInt64();
                for (int i = 0; i < 8 && count > 0; i++, count--)
                {
                    buffer[offset++] = (byte)(v >> (8 * i));
                }
            }
        }
    }

    /// <summary>Outcome of one HTTP request. Status 0 = no HTTP answer (network error, timeout).</summary>
    public struct PulseHttpResult
    {
        public int Status;
        /// <summary>Retry-After in seconds when the server sent one, else -1.</summary>
        public int RetryAfterSeconds;
        /// <summary>Response body (small JSON) or null.</summary>
        public string Body;
        public string Error;
    }

    public interface IPulseTransport
    {
        /// <summary>
        /// POST <paramref name="length"/> bytes of <paramref name="body"/> to <paramref name="url"/>
        /// with Content-Type application/json, X-Pulse-Key and, when <paramref name="gzip"/>,
        /// Content-Encoding: gzip. Call <paramref name="done"/> exactly once, from any thread.
        /// The core never reuses <paramref name="body"/> before <paramref name="done"/> runs.
        /// </summary>
        void Post(string url, string writeKey, byte[] body, int length, bool gzip, Action<PulseHttpResult> done);
    }

    /// <summary>What the host app and device are. The adapter (Unity, a console app, a test) fills it once per launch.</summary>
    public sealed class PulseDevice
    {
        public string AppId = "";
        public string Platform = PulseContract.Platform.Editor;
        public string AppVersion = "0";
        public string Build;
        public string BuildEnv = PulseContract.BuildEnv.Release;
        public string InstallStore = PulseContract.InstallStore.Unknown;
        public string InstallerRaw;
        public string AppName;
        public string Sdk = PulseCore.SdkName;
        public string Engine;
        public string Os;
        public string DeviceModel;
        public string DeviceTier;
        public int RamMb = -1;
        public string Locale;
        public int TzOffsetMin = int.MinValue;
    }

    /// <summary>The reason the previous process ended, as the OS reported it (Android ApplicationExitInfo).</summary>
    public sealed class PulseExitInfo
    {
        /// <summary>One of PulseContract.ExitReason.</summary>
        public string Reason;
        public long TimestampMs;
        public long RssKb = -1;
    }

    /// <summary>Platform services the core calls. Every member is optional except DataDirectory.</summary>
    public sealed class PulseEnvironment
    {
        /// <summary>Directory for the state file and the event queue (created when missing).</summary>
        public string DataDirectory;

        /// <summary>
        /// A directory the OS never restores from a backup (Android getNoBackupFilesDir()). A marker
        /// written there tells a restored install_id from the original install. Null: no restore check.
        /// </summary>
        public string NoBackupDirectory;

        /// <summary>Excludes a file from device backups (iOS NSURLIsExcludedFromBackupKey). Optional.</summary>
        public Action<string> ExcludeFromBackup;

        /// <summary>The OS-reported reason the previous process ended (Android 11+), or null.</summary>
        public Func<PulseExitInfo> PreviousExit;

        public IPulseClock Clock;
        public IPulseRandom Random;
        public IPulseTransport Transport;

        /// <summary>Diagnostics (warnings); never called on the per-event path. Null: silent.</summary>
        public Action<string> Log;
    }
}
