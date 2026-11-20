using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;

namespace Studio.Pulse.Tests
{
    /// <summary>
    /// level_attempt (PulseAttempt), the v1 tap trace (PulseTrace) and the per-install sample. The
    /// vectors are the ones Acorn Mart 1.0.1's own tests used (DenemeAnalitikTest), so the SDK sends
    /// what that game sent; the golden strings pin the wire format the lab decodes.
    /// </summary>
    public static class AttemptTests
    {
        [PulseTest]
        public static void Trace_round_trips_taps_and_times()
        {
            var r = new Random(7);
            const int n = 137;
            var taps = new int[n];
            var times = new float[n];
            float t = 0f;
            for (int i = 0; i < n; i++)
            {
                taps[i] = r.Next(5) == 0 ? -1 : r.Next(0, 4095);
                t += (float)(r.NextDouble() * 6.0);
                times[i] = t;
            }
            Check.Equal(PulseTrace.V1, PulseTrace.Encode(taps, times, out string[] tp, out string[] dp), "status");
            Check.True(tp.Concat(dp).Where(s => s != null).All(s => s.Length <= PulseContract.Limits.StringMaxLen), "a part fits 100 characters");
            Check.Equal(3, tp.Count(s => s != null), "137 taps: 3 parts");
            PulseTrace.Decode(tp, dp, out int[] taps2, out float[] times2);
            Check.True(taps2.SequenceEqual(taps), "taps exactly");
            for (int i = 0; i < n; i++)
                Check.True(Math.Abs(times2[i] - times[i]) <= 0.051f, "tap " + i + " time (0.1 s steps, errors do not add up): " + times[i] + " vs " + times2[i]);
        }

        [PulseTest]
        public static void Trace_cuts_long_attempts_and_refuses_ids_that_do_not_fit()
        {
            int[] longOne = Enumerable.Range(0, 301).ToArray();
            Check.Equal(PulseTrace.Truncated, PulseTrace.Encode(longOne, longOne.Select(x => x * 1f).ToArray(), out string[] tp, out string[] dp), "301 taps");
            PulseTrace.Decode(tp, dp, out int[] back, out float[] _);
            Check.True(back.SequenceEqual(longOne.Take(300)), "the first 300 are kept");

            Check.Equal(PulseTrace.V1, PulseTrace.Encode(new[] { 4094 }, new[] { 1f }, out tp, out dp), "the largest id fits");
            Check.Equal(PulseTrace.Broken, PulseTrace.Encode(new[] { 4095 }, new[] { 1f }, out tp, out dp), "4095 does not");
            Check.True(tp.All(s => s == null) && dp.All(s => s == null), "a broken trace sends no part");
            Check.Equal(PulseTrace.Broken, PulseTrace.Encode(new[] { 3, -2 }, new[] { 1f, 2f }, out tp, out dp), "below -1");
            Check.Equal(PulseTrace.V1, PulseTrace.Encode(null, null, out tp, out dp), "no taps");
            Check.True(tp.All(s => s == null), "no taps: no part");
        }

        [PulseTest]
        public static void Trace_wire_format_is_fixed()
        {
            // v = tap + 1 in two letters of the alphabet; times as 0.1 s gaps (4, 6, 0, 4090)
            string status = PulseTrace.Encode(new[] { 0, 5, -1, 4094 }, new[] { 0.4f, 1.0f, 1.05f, 410f }, out string[] t, out string[] d);
            Check.Equal("v1", status, "status");
            Check.Equal("ABAGAA__", t[0], "taps");
            Check.Equal("AEAGAA_6", d[0], "times");
            Check.Equal("v1-kesik", PulseTrace.Truncated, "the truncated status is a wire value");
            Check.Equal("v1-bozuk", PulseTrace.Broken, "the broken status is a wire value");
            // a gap longer than 409.5 s is clipped; times that go back count as 0
            PulseTrace.Encode(new[] { 1, 2, 3 }, new[] { 500f, 499f, 499.5f }, out t, out d);
            Check.Equal("__AAAF", d[0], "clipped and never negative");
            // fewer times than taps: the last time repeats
            PulseTrace.Encode(new[] { 1, 2, 3 }, new[] { 2f }, out t, out d);
            Check.Equal("AUAAAA", d[0], "missing times");
        }

        static PulseAttempt Long()
        {
            return new PulseAttempt
            {
                Level = 49, Table = 49, Band = "boss", Try = 3, Won = false, Reason = "basket_full",
                Continues = 1, Boosters = 44, Progress = 0.83f, Seconds = 412.5f,
                Taps = Enumerable.Range(0, 400).Select(i => i % 9 == 0 ? -1 : 1000 + i).ToArray(),
                TapTimes = Enumerable.Range(0, 400).Select(i => i * 1.1f).ToArray(),
            };
        }

        static object Value(List<KeyValuePair<string, object>> l, string key)
        {
            return l.Single(x => x.Key == key).Value;
        }

        [PulseTest]
        public static void Attempt_fits_the_event_limits()
        {
            var withTrace = Long().Fields(true);
            Check.True(withTrace.Count <= PulseParams.Capacity, "at most 25 parameters: " + withTrace.Count);
            Check.Equal(withTrace.Count, withTrace.Select(a => a.Key).Distinct().Count(), "no parameter twice");
            Check.True(PulseCore.ValidName(PulseAttempt.EventName), "event name");
            foreach (var a in withTrace)
            {
                Check.True(PulseCore.ValidName(a.Key), "parameter name " + a.Key);
                if (a.Value is string s) Check.True(s.Length <= PulseContract.Limits.StringMaxLen, a.Key + " length");
            }
            int bytes = withTrace.Sum(a => a.Key.Length + 6 + Convert.ToString(a.Value, CultureInfo.InvariantCulture).Length);
            Check.True(bytes < PulseContract.Limits.EventMaxBytes / 4, "far below the 8 KB event limit: " + bytes);
            Check.Equal(PulseTrace.Truncated, Value(withTrace, "trace"), "400 taps are cut");
            Check.Equal(24, withTrace.Count, "11 summary + trace + 6 tap parts + 6 time parts");
            Check.Equal(355, Value(withTrace, "moves"), "moves: taps that are not -1");
            Check.Equal(44, Value(withTrace, "boosters"), "boosters as the game counts them");

            var summary = Long().Fields(false);
            Check.Equal(11, summary.Count, "without the trace only the summary");
            Check.True(!summary.Any(a => a.Key == "trace" || a.Key == "t0" || a.Key == "d0"), "no trace parameter");
        }

        [PulseTest]
        public static void Attempt_order_and_types_are_the_ones_sent()
        {
            var a = new PulseAttempt { Level = 7, Table = 12, Band = "normal", Try = 2, Won = true, Seconds = 12.34f, Progress = 0.12345, Taps = new[] { 3, -1, 4 }, TapTimes = new[] { 1f, 2f, 3f } };
            var f = a.Fields(true);
            Check.Equal("level,table,band,try,won,reason,continues,boosters,moves,seconds,progress,trace,t0,d0",
                string.Join(",", f.Select(x => x.Key)), "order");
            Check.True(Value(f, "level") is long && Value(f, "table") is long, "level and table are integers");
            Check.True(Value(f, "try") is int && Value(f, "continues") is int && Value(f, "boosters") is int && Value(f, "moves") is int, "counts");
            Check.True(Value(f, "won") is bool, "won is a bool");
            Check.Equal("won", Value(f, "reason"), "a won attempt without a reason");
            Check.Equal(12.3, Value(f, "seconds"), "seconds rounded to 0.1");
            Check.Equal(0.123, Value(f, "progress"), "progress rounded to 0.001");
            Check.Equal(2, Value(f, "moves"), "moves from the taps");

            var lost = new PulseAttempt { Level = 1, Moves = 9 }.Fields(false);
            Check.Equal("unknown", Value(lost, "reason"), "a lost attempt without a reason");
            Check.Equal(9, Value(lost, "moves"), "moves given by the game win");
            Check.True(!lost.Any(x => x.Key == "table" || x.Key == "band"), "table and band only when set");

        }

        [PulseTest]
        public static void Level_attempt_reaches_the_collector()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                var a = new PulseAttempt { Level = 3, Table = 3, Band = "normal", Try = 1, Won = false, Reason = "out_of_time", Boosters = 1, Seconds = 30, Progress = 0.5, Taps = new[] { 12, -1 }, TapTimes = new[] { 1.2f, 1.2f } };
                Check.True(core.LevelAttempt(a, true), "with the trace");
                Check.True(core.LevelAttempt(a, false), "without");
                Check.True(!core.LevelAttempt(null, true), "null");
                core.Flush();
                rig.Drain();
                var rows = rig.Collector.Rows.Where(r => (string)r["name"] == PulseAttempt.EventName).ToList();
                Check.Equal(2, rows.Count, "two events");
                var p = (Dictionary<string, object>)rows[0]["p"];
                Check.Equal("v1", p["trace"], "trace status");
                Check.Equal("ANAA", p["t0"], "taps");
                Check.Equal("AMAA", p["d0"], "times");
                Check.True(p["won"] is bool won && !won, "won false");
                Check.Equal("out_of_time", p["reason"], "reason");
                Check.Equal(1L, Convert.ToInt64(p["moves"]), "moves");
                var q = (Dictionary<string, object>)rows[1]["p"];
                Check.True(!q.ContainsKey("trace") && !q.ContainsKey("t0"), "no trace outside the sample (the reused parameters were cleared)");
                Check.Equal(0L, core.GetStats().Rejected, "nothing refused");
            }
        }

        [PulseTest]
        public static void Install_sample_is_chosen_once_and_kept()
        {
            int calls = 0;
            Func<double> low = () => { calls++; return 0.1; };
            Func<double> high = () => { calls++; return 0.3; };
            Check.True(PulseInstallSample.Resolve(-1, 0.25f, low, out int store) && store == 1, "not chosen, 0.1 < 0.25: in");
            Check.True(!PulseInstallSample.Resolve(-1, 0.25f, high, out store) && store == 0, "0.3: out");
            calls = 0;
            Check.True(PulseInstallSample.Resolve(1, 0.25f, high, out store) && store == 1, "kept in");
            Check.True(!PulseInstallSample.Resolve(0, 0.25f, low, out store) && store == 0, "kept out");
            Check.True(!PulseInstallSample.Resolve(2, 0.25f, low, out store) && store == 2, "any other kept value is out");
            Check.Equal(0, calls, "a kept choice draws nothing");

            var r = new Random(11);
            int inside = 0;
            for (int i = 0; i < 20000; i++) if (PulseInstallSample.Resolve(-1, PulseAttempt.DefaultTraceRate, r.NextDouble, out store)) inside++;
            Check.InRange(inside, 4600, 5400, "about a quarter of installs");
        }
    }
}
