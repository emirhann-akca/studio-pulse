// Experiment assignment: GrowthBook's own cases (contract/golden/assign_growthbook.json),
// shared with the Python and Unity implementations, and the exposure event of the core.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:studio_pulse/core.dart';

import 'support/kit.dart';

Map<String, Object?> golden() {
  var dir = Directory.current;
  while (!File('${dir.path}/contract/golden/assign_growthbook.json')
      .existsSync()) {
    dir = dir.parent;
  }
  return jsonDecode(File('${dir.path}/contract/golden/assign_growthbook.json')
      .readAsStringSync()) as Map<String, Object?>;
}

Map<String, Object?> p(Map<String, Object?> row) =>
    row['p']! as Map<String, Object?>;

void main() {
  final g = golden();

  test('hash matches GrowthBook', () {
    for (final c in (g['hash']! as List<Object?>).cast<List<Object?>>()) {
      final got = pulseHash(c[0] as String, c[1] as String, c[2] as int);
      final want = c[3] as num?;
      if (want == null) {
        expect(got, isNull, reason: '$c');
      } else {
        expect(got, closeTo(want.toDouble(), 1e-9), reason: '$c');
      }
    }
  });

  test('bucket ranges and variations match GrowthBook', () {
    for (final c
        in (g['getBucketRange']! as List<Object?>).cast<List<Object?>>()) {
      final args = c[1]! as List<Object?>;
      final w = (args[2] as List<Object?>?)
          ?.map((x) => (x as num).toDouble())
          .toList();
      final got =
          pulseBucketRanges(args[0] as int, (args[1] as num).toDouble(), w);
      final want = (c[2]! as List<Object?>).cast<List<Object?>>();
      expect(got.length, want.length, reason: c[0] as String);
      for (var i = 0; i < want.length; i++) {
        expect(got[i][0], closeTo((want[i][0] as num).toDouble(), 1e-9),
            reason: c[0] as String);
        expect(got[i][1], closeTo((want[i][1] as num).toDouble(), 1e-9),
            reason: c[0] as String);
      }
    }
    for (final c
        in (g['chooseVariation']! as List<Object?>).cast<List<Object?>>()) {
      final ranges = (c[2] as List)
          .cast<List<Object?>>()
          .map((r) => r.map((x) => (x as num).toDouble()).toList())
          .toList();
      expect(pulseChooseVariation((c[1] as num).toDouble(), ranges), c[3],
          reason: c[0] as String);
    }
    for (final c
        in (g['getEqualWeights']! as List<Object?>).cast<List<Object?>>()) {
      final want =
          (c[1]! as List<Object?>).map((x) => (x! as num).toDouble()).toList();
      final got = pulseEqualWeights(c[0] as int);
      expect(got.length, want.length);
      for (var i = 0; i < want.length; i++) {
        expect(
            got[i], closeTo(want[i], 1e-6)); // the cases round 1/3 to 8 places
      }
    }
  });

  group('core', () {
    late Rig rig;
    setUp(() => rig = Rig());
    tearDown(() => rig.dispose());

    test('one exposure per session and key, the same variation every time', () {
      final core = rig.start();
      final v = core.experiment('booster_offer');
      expect(v, anyOf(0, 1));
      expect(core.experiment('booster_offer'), v);
      expect(
          core.experiment('booster_offer',
              variations: 3, weights: <double>[0.2, 0.3, 0.5]),
          pulseAssign('booster_offer', core.installId!,
              variations: 3, weights: <double>[0.2, 0.3, 0.5]).$1);
      final out = core.experiment('tiny_test', coverage: 0);
      expect(out, -1);
      core.flush();
      rig.drain();
      final rows = rig.fake.collector.named('experiment_exposure');
      expect(rows.length, 2); // booster_offer once, tiny_test once
      final first = p(rows.first);
      expect(first['experiment_key'], 'booster_offer');
      expect(first['variation'], v);
      expect(first['in_experiment'], true);
      expect(first['hash_value'],
          closeTo(pulseHash('booster_offer', core.installId!, 2)!, 1e-9));
      expect(p(rows.last)['in_experiment'], false);
    });
  });
}
