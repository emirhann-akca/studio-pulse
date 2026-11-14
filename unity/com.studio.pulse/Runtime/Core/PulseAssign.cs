// Deterministic experiment assignment: GrowthBook's algorithm (MIT,
// https://github.com/growthbook/growthbook), hash version 2 over UTF-16 code units, the same as
// studio_pulse/assign.py and flutter/studio_pulse/lib/src/core/assign.dart. Tested against
// GrowthBook's own cases in contract/golden/assign_growthbook.json.

using System.Globalization;

namespace Studio.Pulse
{
    public static class PulseAssign
    {
        public static uint Fnv1a32(string text)
        {
            uint h = 0x811c9dc5;
            for (int i = 0; i < text.Length; i++) h = unchecked((h ^ text[i]) * 0x01000193u);
            return h;
        }

        /// <summary>GrowthBook's hash: v2 = fnv(fnv(seed + value).ToString()) % 10000 / 10000; v1 kept for its cases; other versions null.</summary>
        public static double? Hash(string seed, string value, int version)
        {
            if (version == 2) return (Fnv1a32(Fnv1a32(seed + value).ToString(CultureInfo.InvariantCulture)) % 10000) / 10000.0;
            if (version == 1) return (Fnv1a32(value + seed) % 1000) / 1000.0;
            return null;
        }

        public static double[] EqualWeights(int n)
        {
            if (n <= 0) return new double[0];
            var w = new double[n];
            for (int i = 0; i < n; i++) w[i] = 1.0 / n;
            return w;
        }

        public static double[][] BucketRanges(int n, double coverage, double[] weights)
        {
            double c = coverage < 0 ? 0 : (coverage > 1 ? 1 : coverage);
            double[] equal = EqualWeights(n);
            double[] w = weights ?? equal;
            if (w.Length != n) w = equal;
            double total = 0;
            foreach (double x in w) total += x;
            if (total < 0.99 || total > 1.01) w = equal;
            var ranges = new double[w.Length][];
            double cumulative = 0;
            for (int i = 0; i < w.Length; i++)
            {
                double start = cumulative;
                cumulative += w[i];
                ranges[i] = new[] { start, start + c * w[i] };
            }
            return ranges;
        }

        public static int ChooseVariation(double bucket, double[][] ranges)
        {
            for (int i = 0; i < ranges.Length; i++)
                if (bucket >= ranges[i][0] && bucket < ranges[i][1]) return i;
            return -1;
        }

        /// <summary>The variation (-1 outside the coverage) and the install's bucket in [0, 1).</summary>
        public static int Assign(string key, string installId, out double bucket, int variations = 2, double[] weights = null, double coverage = 1)
        {
            bucket = Hash(key, installId, 2).Value;
            return ChooseVariation(bucket, BucketRanges(variations, coverage, weights));
        }
    }
}
