// Pure C# (no UnityEngine): the v1 tap trace of a level attempt (level_attempt: trace, t0-t5,
// d0-d5; docs/SDK_UNITY.md "Level attempts"). First shipped by Acorn Mart 1.0.1 (19 Nov 2026):
// the alphabet, the part sizes and the status strings are wire values the lab decodes, so they
// never change; a new layout gets a new version string.
using System;
using System.Collections.Generic;
using System.Text;

namespace Studio.Pulse
{
    /// <summary>
    /// The taps of one attempt as short strings that fit the event limits. Every value is two
    /// characters of a 64-letter alphabet (value = first * 64 + second, 0-4095). Taps: the move id
    /// + 1 (a tap that is not a move, <see cref="NotAMove"/>, is 0). Times: each tap's time since
    /// the level started, rounded to 0.1 s, written as the gap to the previous tap (the first from
    /// the start), so rounding errors do not add up; a gap longer than 409.5 s is clipped. Parts of
    /// 50 taps (100 characters, the string limit), at most 6 parts: 300 taps. Longer attempts are
    /// cut (<see cref="Truncated"/>); a move id that does not fit sends no trace (<see cref="Broken"/>).
    /// </summary>
    public static class PulseTrace
    {
        public const int Parts = 6;
        public const int PartTaps = 50;
        public const int MaxTaps = Parts * PartTaps;
        /// <summary>The largest encoded value (a move id up to 4094 fits).</summary>
        public const int MaxValue = 64 * 64 - 1;
        /// <summary>A tap that is not a move (a booster, a continue).</summary>
        public const int NotAMove = -1;

        public const string V1 = "v1";
        /// <summary>More than <see cref="MaxTaps"/> taps: the first 300 are sent.</summary>
        public const string Truncated = "v1-kesik";
        /// <summary>A move id outside 0-4094 (or below -1): only the status is sent.</summary>
        public const string Broken = "v1-bozuk";

        public const string Alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

        /// <summary>The parameter that carries the status (V1, Truncated, Broken).</summary>
        public const string StatusKey = "trace";

        /// <summary>The parameter of tap part <paramref name="part"/> (t0 ... t5).</summary>
        public static string TapsKey(int part) { return "t" + part; }

        /// <summary>The parameter of time part <paramref name="part"/> (d0 ... d5).</summary>
        public static string TimesKey(int part) { return "d" + part; }

        /// <summary>
        /// Encodes taps and their times (seconds since the level started; a shorter array repeats its
        /// last time, an empty one gives 0). Returns the status; <paramref name="t"/> and
        /// <paramref name="d"/> hold <see cref="Parts"/> strings, unused parts null. Null arrays are empty.
        /// </summary>
        public static string Encode(int[] taps, float[] times, out string[] t, out string[] d)
        {
            taps = taps ?? new int[0];
            times = times ?? new float[0];
            t = new string[Parts];
            d = new string[Parts];
            int n = Math.Min(taps.Length, MaxTaps);
            var st = new StringBuilder(2 * PartTaps);
            var sd = new StringBuilder(2 * PartTaps);
            long previous = 0;
            for (int i = 0; i < n; i++)
            {
                int v = taps[i] + 1;
                if (v < 0 || v > MaxValue)
                {
                    t = new string[Parts];
                    d = new string[Parts];
                    return Broken;
                }
                Two(st, v);
                float z = times.Length == 0 ? 0f : times[Math.Min(i, times.Length - 1)];
                long q = (long)Math.Round(z * 10.0);
                Two(sd, (int)Math.Max(0, Math.Min(MaxValue, q - previous)));
                previous = q;
                if ((i + 1) % PartTaps == 0 || i == n - 1)
                {
                    int p = i / PartTaps;
                    t[p] = st.ToString();
                    d[p] = sd.ToString();
                    st.Length = 0;
                    sd.Length = 0;
                }
            }
            return taps.Length > n ? Truncated : V1;
        }

        static void Two(StringBuilder s, int v)
        {
            s.Append(Alphabet[v >> 6]).Append(Alphabet[v & 63]);
        }

        /// <summary>
        /// The inverse of <see cref="Encode"/> (lab, tests): parts in order until the first empty one.
        /// Times come back as seconds since the level started (0.1 s steps).
        /// </summary>
        public static void Decode(string[] t, string[] d, out int[] taps, out float[] times)
        {
            var lt = new List<int>();
            var lz = new List<float>();
            long sum = 0;
            for (int p = 0; t != null && p < t.Length && !string.IsNullOrEmpty(t[p]); p++)
            {
                string dp = d != null && p < d.Length ? d[p] ?? "" : "";
                for (int j = 0; j + 1 < t[p].Length; j += 2)
                {
                    lt.Add(Alphabet.IndexOf(t[p][j]) * 64 + Alphabet.IndexOf(t[p][j + 1]) - 1);
                    if (j + 1 < dp.Length) sum += Alphabet.IndexOf(dp[j]) * 64 + Alphabet.IndexOf(dp[j + 1]);
                    lz.Add(sum / 10f);
                }
            }
            taps = lt.ToArray();
            times = lz.ToArray();
        }
    }
}
