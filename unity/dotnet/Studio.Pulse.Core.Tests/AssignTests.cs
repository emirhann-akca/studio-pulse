using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace Studio.Pulse.Tests
{
    /// <summary>
    /// GrowthBook's own assignment cases (contract/golden/assign_growthbook.json, MIT); the
    /// Python and Dart implementations run the same file, so the three agree on every install.
    /// </summary>
    public static class AssignTests
    {
        static Dictionary<string, object> Golden()
        {
            string path = PulseTestRunner.FindRepoFile("contract/golden/assign_growthbook.json");
            return (Dictionary<string, object>)MiniJson.Parse(File.ReadAllText(path, Encoding.UTF8));
        }

        static double[] Doubles(object o) { return ((List<object>)o).Select(Convert.ToDouble).ToArray(); }

        static void Near(double expected, double actual, string what)
        {
            Check.True(Math.Abs(expected - actual) < 1e-6, what + ": expected " + expected + " got " + actual);
        }

        [PulseTest]
        public static void Hash_matches_growthbook()
        {
            int n = 0;
            foreach (List<object> c in (List<object>)Golden()["hash"])
            {
                double? got = PulseAssign.Hash((string)c[0], (string)c[1], Convert.ToInt32(c[2]));
                if (c[3] == null) Check.True(got == null, "unknown version -> null");
                else Near(Convert.ToDouble(c[3]), got.Value, "hash(" + c[0] + ", " + c[1] + ", v" + c[2] + ")");
                n++;
            }
            Check.True(n >= 20, "the golden file has its cases (" + n + ")");
        }

        [PulseTest]
        public static void Bucket_ranges_and_variation_choice_match_growthbook()
        {
            var g = Golden();
            foreach (List<object> c in (List<object>)g["getBucketRange"])
            {
                var args = (List<object>)c[1];
                double[] w = args[2] == null ? null : Doubles(args[2]);
                double[][] got = PulseAssign.BucketRanges(Convert.ToInt32(args[0]), Convert.ToDouble(args[1]), w);
                var want = (List<object>)c[2];
                Check.Equal(want.Count, got.Length, (string)c[0] + ": range count");
                for (int i = 0; i < want.Count; i++)
                {
                    double[] r = Doubles(want[i]);
                    Near(r[0], got[i][0], (string)c[0] + " start " + i);
                    Near(r[1], got[i][1], (string)c[0] + " end " + i);
                }
            }
            foreach (List<object> c in (List<object>)g["chooseVariation"])
            {
                double[][] ranges = ((List<object>)c[2]).Select(Doubles).ToArray();
                Check.Equal(Convert.ToInt32(c[3]), PulseAssign.ChooseVariation(Convert.ToDouble(c[1]), ranges), (string)c[0]);
            }
            foreach (List<object> c in (List<object>)g["getEqualWeights"])
            {
                double[] got = PulseAssign.EqualWeights(Convert.ToInt32(c[0]));
                double[] want = Doubles(c[1]);
                Check.Equal(want.Length, got.Length, "equal weights " + c[0]);
                for (int i = 0; i < want.Length; i++) Near(want[i], got[i], "equal weight " + i);
            }
        }

        [PulseTest]
        public static void Split_is_even_and_stable_across_installs()
        {
            int[] count = new int[2];
            for (int i = 0; i < 10000; i++)
            {
                string id = "install-" + i;
                int v = PulseAssign.Assign("onboarding_v2", id, out double b);
                int again = PulseAssign.Assign("onboarding_v2", id, out double b2);
                Check.True(v == again && b == b2, "same install, same answer");
                count[v]++;
            }
            Check.InRange(count[0], 4800, 5200, "control share of 10000");
            int outside = 0;
            for (int i = 0; i < 10000; i++) if (PulseAssign.Assign("half", "i" + i, out _, 2, null, 0.5) < 0) outside++;
            Check.InRange(outside, 4800, 5200, "coverage 0.5 leaves half outside");
        }

        [PulseTest]
        public static void Experiment_logs_one_exposure_per_session_and_key()
        {
            using (var rig = new Rig())
            {
                PulseCore core = rig.Start();
                string id = core.GetInstallId();
                int v = core.Experiment("booster_offer");
                Check.True(v == 0 || v == 1, "a variation of two");
                Check.Equal(v, core.Experiment("booster_offer"), "stable within the session");
                int three = core.Experiment("booster_offer", 3, new[] { 0.2, 0.3, 0.5 });
                Check.Equal(PulseAssign.Assign("booster_offer", id, out _, 3, new[] { 0.2, 0.3, 0.5 }), three, "weights reach the assignment");
                Check.Equal(-1, core.Experiment("tiny_test", 2, null, 0), "coverage 0 -> outside");
                Check.Equal(-1, core.Experiment("", 2), "empty key -> -1");
                Check.Equal(-1, core.Experiment("too_many", 33), "more than 32 variations -> -1");
                core.Flush();
                rig.Drain();
                var rows = rig.Collector.Rows.Where(r => (string)r["name"] == "experiment_exposure").ToList();
                Check.Equal(2, rows.Count, "booster_offer once, tiny_test once");
                var p = (Dictionary<string, object>)rows[0]["p"];
                Check.Equal("booster_offer", (string)p["experiment_key"], "key");
                Check.Equal((long)v, Convert.ToInt64(p["variation"]), "variation");
                Check.Equal(true, (bool)p["in_experiment"], "in experiment");
                Near(PulseAssign.Hash("booster_offer", id, 2).Value, Convert.ToDouble(p["hash_value"]), "bucket");
                Check.Equal(false, (bool)((Dictionary<string, object>)rows[1]["p"])["in_experiment"], "outside the coverage");
            }
        }

        [PulseTest]
        public static void Experiment_before_consent_assigns_nothing()
        {
            using (var rig = new Rig())
            {
                rig.Options.ConsentRequired = true;
                PulseCore core = rig.Start();
                Check.Equal(-1, core.Experiment("booster_offer"), "no install id before consent -> control");
            }
        }
    }
}
