// Deterministic experiment assignment: GrowthBook's algorithm (MIT,
// https://github.com/growthbook/growthbook), hash version 2 over UTF-16 code units, the same
// as studio_pulse/assign.py and Runtime/Core/PulseAssign.cs. Tested against GrowthBook's own
// cases in contract/golden/assign_growthbook.json.

int pulseFnv1a32(String text) {
  var h = 0x811c9dc5;
  for (final unit in text.codeUnits) {
    h = ((h ^ unit) * 0x01000193) & 0xffffffff;
  }
  return h;
}

/// GrowthBook's hash: v2 = fnv(fnv(seed + value).toString()) % 10000 / 10000 (v1 for its cases).
double? pulseHash(String seed, String value, int version) {
  if (version == 2)
    return (pulseFnv1a32(pulseFnv1a32(seed + value).toString()) % 10000) /
        10000;
  if (version == 1) return (pulseFnv1a32(value + seed) % 1000) / 1000;
  return null;
}

List<double> pulseEqualWeights(int n) =>
    n <= 0 ? <double>[] : List<double>.filled(n, 1 / n);

List<List<double>> pulseBucketRanges(
    int n, double coverage, List<double>? weights) {
  final c = coverage < 0 ? 0.0 : (coverage > 1 ? 1.0 : coverage);
  final equal = pulseEqualWeights(n);
  var w = weights ?? equal;
  if (w.length != n) w = equal;
  final total = w.fold<double>(0, (a, b) => a + b);
  if (total < 0.99 || total > 1.01) w = equal;
  var cumulative = 0.0;
  return [
    for (final x in w)
      () {
        final start = cumulative;
        cumulative += x;
        return <double>[start, start + c * x];
      }(),
  ];
}

int pulseChooseVariation(double bucket, List<List<double>> ranges) {
  for (var i = 0; i < ranges.length; i++) {
    if (bucket >= ranges[i][0] && bucket < ranges[i][1]) return i;
  }
  return -1;
}

/// (variation or -1 outside the coverage, the install's bucket in [0, 1)).
(int, double) pulseAssign(String key, String installId,
    {int variations = 2, List<double>? weights, double coverage = 1}) {
  final bucket = pulseHash(key, installId, 2)!;
  return (
    pulseChooseVariation(
        bucket, pulseBucketRanges(variations, coverage, weights)),
    bucket
  );
}
