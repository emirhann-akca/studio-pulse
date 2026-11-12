// Cross-SDK conformance: replays contract/golden/sdk_script.json with a fake clock and
// SplitMix64; the request bodies must equal contract/golden/sdk_conformance.ndjson byte
// for byte, the file the Unity SDK (unity/dotnet ConformanceTests) also produces.
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:studio_pulse/core.dart';

import 'support/kit.dart';

PulseEventParams params(List<Object?> list) {
  final p = PulseEventParams();
  for (final t in list.cast<List<Object?>>()) {
    final key = t[0]! as String;
    switch (t[1]) {
      case 'i':
        p.addInt(key, (t[2]! as num).toInt());
      case 'd':
        p.addDouble(key, (t[2]! as num).toDouble());
      case 'b':
        p.addBool(key, t[2]! as bool);
      default:
        p.addString(key, t[2]! as String);
    }
  }
  return p;
}

List<String> replay(String scriptPath) {
  final script = jsonDecode(File(scriptPath).readAsStringSync()) as Map<String, Object?>;
  final start = script['start_ms']! as int;
  final clock = FakeClock(start);
  final rnd = SeededRandom(script['seed']! as int);
  final transport = FakeTransport();
  final d = script['device']! as Map<String, Object?>;
  final device = PulseDevice(
    appId: d['app_id']! as String,
    platform: d['platform']! as String,
    appVersion: d['app_version']! as String,
    build: d['build'] as String?,
    buildEnv: d['build_env']! as String,
    installStore: d['install_store']! as String,
    installerRaw: d['installer_raw'] as String?,
    appName: d['app_name'] as String?,
    sdk: d['sdk'] as String?,
    engine: d['engine'] as String?,
    os: d['os'] as String?,
    deviceModel: d['device_model'] as String?,
    deviceTier: d['device_tier'] as String?,
    ramMb: d['ram_mb']! as int,
    locale: d['locale'] as String?,
    tzOffsetMin: d['tz_offset_min'] as int?,
  );
  final dir = Directory.systemTemp.createTempSync('pulse-conf-');
  PulseCore? core;
  PulseCore newCore() => PulseCore(script['write_key']! as String, script['endpoint']! as String, PulseOptions(), device,
      PulseEnvironment(dataDirectory: dir.path, clock: clock, random: rnd, transport: transport))
    ..start();
  try {
    for (final step in (script['steps']! as List<Object?>).cast<Map<String, Object?>>()) {
      final at = start + (step['at']! as int);
      if (at > clock.now) clock.advance(at - clock.now);
      core?.tick();
      final a = (step['args'] as Map<String, Object?>?) ?? <String, Object?>{};
      String? s(String k) => a[k] as String?;
      int? i(String k) => (a[k] as num?)?.toInt();
      double? f(String k) => (a[k] as num?)?.toDouble();
      bool? b(String k) => a[k] as bool?;
      final c = core;
      switch (step['call']) {
        case 'start':
          core = newCore();
        case 'restart':
          device
            ..appVersion = s('app_version') ?? device.appVersion
            ..build = s('build') ?? device.build;
          core = newCore();
        case 'tick':
          break;
        case 'flush':
          c!.flush();
        case 'background':
          c!.onBackground();
        case 'foreground':
          c!.onForeground();
        case 'set_consent':
          c!.setConsent(b('granted')!);
        case 'set_user_id':
          c!.setUserId(s('uid'));
        case 'tutorial_begin':
          c!.tutorialBegin(s('tutorial'));
        case 'tutorial_step':
          c!.tutorialStep(i('step')!, stepName: s('step_name'), tutorial: s('tutorial'));
        case 'tutorial_complete':
          c!.tutorialComplete(s('tutorial'));
        case 'level_start':
          c!.levelStart(i('level')!, levelName: s('level_name'), world: s('world'), mode: s('mode'), attempt: i('attempt') ?? 0);
        case 'level_complete':
          c!.levelComplete(i('level')!, score: i('score'), stars: i('stars'), movesLeft: i('moves_left'));
        case 'level_fail':
          c!.levelFail(i('level')!, s('reason')!,
              posX: f('pos_x'), posY: f('pos_y'), progress: f('progress'), posSpace: s('pos_space'), score: i('score'));
        case 'level_quit':
          c!.levelQuit(i('level')!, s('reason')!, progress: f('progress'), posX: f('pos_x'), posY: f('pos_y'), posSpace: s('pos_space'));
        case 'setting_change':
          c!.settingChange(s('setting')!, s('from'), s('to')!);
        case 'earn_currency':
          c!.earnCurrency(s('name')!, f('value')!, itemName: s('item'), level: i('level'));
        case 'spend_currency':
          c!.spendCurrency(s('name')!, f('value')!, itemName: s('item'), level: i('level'));
        case 'purchase':
          c!.purchase(s('transaction_id')!, s('product_id')!, i('price_micros')!, s('currency')!,
              isRestore: b('is_restore'), isSandbox: b('is_sandbox'));
        case 'ad_impression':
          c!.adImpression(s('ad_platform')!, s('ad_format')!, f('value')!, s('currency')!,
              adSource: s('ad_source'), adUnitName: s('ad_unit_name'), placement: s('placement'), precision: s('precision'));
        case 'ad_event':
          c!.adEvent(s('action')!, s('ad_format')!, placement: s('placement'), errorCode: s('error_code'));
        case 'screen':
          c!.screen(s('name')!, screenClass: s('class'));
        case 'track':
          c!.track(s('name')!, params(a['params']! as List<Object?>));
        case 'perf':
          c!.recordPerf(s('name')!, params(a['params']! as List<Object?>));
        case 'error':
          c!.reportError(s('type'), s('message'), s('stack'), fatal: b('fatal') ?? false);
        default:
          throw StateError('unknown call ${step['call']}');
      }
    }
    core?.close();
    return transport.requests.map((r) => r.json).toList();
  } finally {
    try {
      dir.deleteSync(recursive: true);
    } on Object {
      // best effort
    }
  }
}

void main() {
  test('batches match the cross-SDK golden byte for byte', () {
    final script = repoFile('contract/golden/sdk_script.json');
    final golden = File(repoFile('contract/golden/sdk_conformance.ndjson'))
        .readAsStringSync()
        .replaceAll('\r\n', '\n')
        .split('\n')
        .where((l) => l.isNotEmpty)
        .toList();
    final bodies = replay(script);
    expect(bodies.length, golden.length);
    for (var i = 0; i < golden.length; i++) {
      if (bodies[i] != golden[i]) {
        var at = 0;
        while (at < golden[i].length && at < bodies[i].length && golden[i][at] == bodies[i][at]) {
          at++;
        }
        final from = at > 60 ? at - 60 : 0;
        String snip(String x) => x.substring(from, (from + 160) < x.length ? from + 160 : x.length);
        fail('request ${i + 1} differs at char $at:\n want ...${snip(golden[i])}\n got  ...${snip(bodies[i])}');
      }
    }
    // the bytes too, not just the decoded text
    final goldenBytes = File(repoFile('contract/golden/sdk_conformance.ndjson')).readAsBytesSync();
    expect(utf8.encode('${bodies.join('\n')}\n'), goldenBytes);
    expect(replay(script), bodies); // deterministic
  });

  test('doubles match the shared table (shortest round-trip, ECMAScript layout)', () {
    final table = (jsonDecode(File(repoFile('contract/golden/sdk_numbers.json')).readAsStringSync()) as List<Object?>)
        .cast<List<Object?>>();
    expect(table.length, greaterThan(3000));
    for (final row in table) {
      final hex = row[0]! as String;
      final bits = (int.parse(hex.substring(0, 8), radix: 16) << 32) | int.parse(hex.substring(8), radix: 16);
      final bd = ByteData(8)..setInt64(0, bits);
      final v = bd.getFloat64(0);
      expect(PulseJsonWriter.formatDouble(v), row[1], reason: row[0]! as String);
    }
  });
}
