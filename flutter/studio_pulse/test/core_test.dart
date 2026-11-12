// The Dart core with a fake clock and a synchronous fake transport: sessions, sequence
// numbers, the queue cap, privacy and the event helpers. Same cases as the Unity SDK's
// unity/dotnet/Studio.Pulse.Core.Tests.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:studio_pulse/core.dart';

import 'support/kit.dart';

List<int> range(int from, int to) => <int>[for (var i = from; i <= to; i++) i];

Map<String, Object?> p(Map<String, Object?> row) => row['p']! as Map<String, Object?>;

void main() {
  late Rig rig;
  setUp(() => rig = Rig());
  tearDown(() => rig.dispose());

  group('sessions', () {
    test('roll over after 30 minutes in the background', () {
      final core = rig.start();
      final sid1 = core.sessionId;
      core.levelStart(5);
      rig.step(20000);
      core.onBackground();
      rig.step(29 * 60000);
      core.onForeground();
      expect(core.sessionId, sid1);
      rig.step(10000);
      core.onBackground();
      rig.step(31 * 60000);
      core.onForeground();
      expect(core.sessionId, isNot(sid1));
      core.flush();
      rig.drain();
      final c = rig.fake.collector;
      final starts = c.named('pulse_session_start');
      expect(starts.length, 2);
      expect(p(starts[1])['cold'], false);
      expect(p(starts[1])['since_last_s'], 31 * 60);
      expect(starts[1]['snum'], 2);
      final quit = c.named('level_quit').single;
      expect(p(quit)['reason'], 'background_timeout');
      expect(p(quit)['duration_ms'], 30000);
      expect(quit['sid'], sid1);
      expect(c.named('pulse_foreground').length, 1);
      expect(c.named('pulse_background').map((r) => p(r)['fg_ms']).toList(), <int>[20000, 30000]);
    });

    test('never span more than 24 hours', () {
      final core = rig.start();
      final sid = core.sessionId;
      rig.step(3600000, 25);
      expect(core.sessionId, isNot(sid));
    });

    test('heartbeat only after a quiet minute in the foreground', () {
      final core = rig.start();
      for (var i = 0; i < 10; i++) {
        rig.step(30000);
        core.screen('x');
      }
      rig.step(30000, 4);
      core.onBackground();
      rig.step(300000);
      core.flush();
      rig.drain();
      expect(rig.fake.collector.named('pulse_heartbeat').length, 2);
    });

    test('seq is monotonic across restarts and kills', () {
      var n = 0;
      rig.fake.responder = (s) => (++n % 3 == 0) ? const PulseHttpResult(503) : FakeTransport.ok(s);
      for (var launch = 0; launch < 6; launch++) {
        final core = rig.restart();
        for (var i = 0; i < 25; i++) {
          core.screen('s$i');
        }
        rig.step(5000, 3);
        if (launch.isEven) core.onBackground();
      }
      rig.fake.responder = FakeTransport.ok;
      final last = rig.restart()..flush();
      for (var i = 0; i < 100 && last.stats.queued > 0; i++) {
        rig.step(61000);
      }
      final seqs = rig.fake.collector.seqs();
      expect(seqs, range(1, seqs.length));
      expect(seqs.length, greaterThan(150));
      expect(rig.fake.collector.rows.map((r) => r['install_id']).toSet().length, 1);
    });

    test('install, update and exit reasons', () {
      rig.start().onQuit();
      rig.device.appVersion = '1.5.0';
      rig.restart().levelStart(1);
      rig.previousExit = () => PulseExitInfo(PulseExitReason.crashNative, timestampMs: rig.clock.now, rssKb: 512000);
      rig.step(1000);
      rig.restart().flush();
      rig.drain();
      final c = rig.fake.collector;
      expect(c.named('pulse_install').length, 1);
      expect(p(c.named('pulse_app_update').single)['from_version'], '1.4.0');
      final exit = c.named('pulse_exit').single;
      expect(p(exit)['reason'], 'crash_native');
      expect(p(exit)['prev_version'], '1.5.0');
      expect(p(exit)['rss_kb'], 512000);
      expect(p(exit)['prev_sid'], c.named('level_start').first['sid']);
      expect(exit['sid'], isNot(c.named('level_start').first['sid']));
    });

    test('a restored backup rotates the install id', () {
      rig.noBackupDir = '${rig.dir.path}/nobackup';
      final a = rig.start();
      final id1 = a.installId;
      a.onBackground();
      expect(rig.restart().installId, id1);
      Directory(rig.noBackupDir!).deleteSync(recursive: true);
      final c = rig.restart()..flush();
      rig.drain();
      expect(c.installId, isNot(id1));
      final inst = rig.fake.collector.named('pulse_install');
      expect(inst.length, 2);
      expect(p(inst[1])['restored'], true);
      expect(inst[1]['seq'], 1);
    });

    test('level attempts and foreground-only durations', () {
      final core = rig.start()..levelStart(3);
      rig.step(12000);
      core
        ..levelFail(3, 'hit_spike', posX: 0.1, posY: 0.9, progress: 0.4)
        ..levelStart(3);
      rig.step(5000);
      core.onBackground();
      rig.step(60000);
      core.onForeground();
      rig.step(3000);
      core
        ..levelComplete(3, score: 900, stars: 2)
        ..levelStart(3)
        ..flush();
      rig.drain();
      final lv = rig.fake.collector.rows.where((r) => (r['name']! as String).startsWith('level_')).map(p).toList();
      expect(lv[0]['attempt'], 1);
      expect(lv[1]['duration_ms'], 12000);
      expect(lv[1]['pos_space'], 'norm');
      expect(lv[2]['attempt'], 2);
      expect(lv[3]['duration_ms'], 8000);
      expect(lv[3]['attempt'], 2);
      expect(lv[4]['attempt'], 1);
    });
  });

  group('queue', () {
    test('flush triggers: 20 events, 30 seconds, background', () {
      final core = rig.start();
      rig.step(1000);
      expect(rig.fake.batches.length, 0);
      for (var i = 0; i < 18; i++) {
        core.screen('s');
      }
      rig.step(1);
      expect(rig.fake.batches.length, 1);
      core.screen('a');
      rig.step(10000);
      expect(rig.fake.batches.length, 1);
      rig.step(20000);
      expect(rig.fake.batches.length, 2);
      core
        ..screen('b')
        ..onBackground();
      expect(rig.fake.batches.length, 3);
      expect(rig.fake.batches[2].json, contains('pulse_background'));
    });

    test('cap drops the oldest segments and counts them', () {
      rig.fake.responder = (s) => const PulseHttpResult(0);
      rig.options
        ..queueMaxEvents = 50
        ..segmentMaxEvents = 10
        ..flushEvents = 100000;
      final core = rig.start();
      for (var i = 0; i < 120; i++) {
        core.screen('s$i');
      }
      final st = core.stats;
      expect(st.queued, lessThanOrEqualTo(50));
      expect(st.dropped, greaterThanOrEqualTo(70));
      expect(st.queued + st.dropped, 122);
      rig.fake.responder = FakeTransport.ok;
      core.flush();
      rig.drain();
      final seqs = rig.fake.collector.seqs();
      expect(seqs.last, 122);
      expect(seqs.first, greaterThan(1));
      expect(rig.restart().stats.dropped, greaterThanOrEqualTo(70));
    });

    test('a torn last line is repaired on load', () {
      rig.fake.responder = (s) => null;
      final a = rig.start();
      a
        ..levelStart(1)
        ..levelComplete(1);
      final seg = rig.queueFiles().firstWhere((f) => f.path.endsWith('.seg'));
      seg.writeAsStringSync('{"id":"0190f0e2-7c1a-7cc3-9b1e-0000000', mode: FileMode.append);
      rig.fake.responder = FakeTransport.ok;
      rig.restart().flush();
      rig.drain();
      expect(rig.fake.collector.rows.every((r) => (r['id']! as String).length == 36), isTrue);
      expect(rig.fake.collector.rows.length, greaterThanOrEqualTo(4));
    });
  });

  group('privacy', () {
    test('opt-out wipes the queue and keeps only the flag', () {
      rig.fake.responder = (s) => const PulseHttpResult(0);
      final core = rig.start();
      for (var i = 0; i < 30; i++) {
        core.screen('s$i');
      }
      expect(rig.queueFiles(), isNotEmpty);
      core.optOut();
      expect(rig.queueFiles(), isEmpty);
      final state = File('${rig.dataDir}/state.json').readAsStringSync();
      expect(state, contains('"opted_out":true'));
      expect(state, isNot(contains('install_id')));
      final before = rig.fake.requests.length;
      core.screen('after');
      rig.fake.responder = FakeTransport.ok;
      core.flush();
      rig.step(120000, 5);
      expect(rig.fake.requests.length, before);
      final again = rig.restart()
        ..screen('next')
        ..flush();
      rig.drain();
      expect(rig.queueFiles(), isEmpty);
      expect(again.stats.optedOut, isTrue);
      expect(again.installId, isNull);
      again
        ..setConsent(true)
        ..flush();
      rig.drain();
      expect(rig.fake.collector.names(), containsAll(<String>['pulse_install', 'pulse_consent']));
    });

    test('consent required: nothing on disk or network until granted', () {
      rig.options.consentRequired = true;
      final core = rig.start()
        ..levelStart(1)
        ..flush();
      rig.step(120000, 3);
      expect(rig.fake.requests, isEmpty);
      final d = Directory(rig.dataDir);
      expect(!d.existsSync() || d.listSync(recursive: true).isEmpty, isTrue);
      expect(core.installId, isNull);
      core
        ..setConsent(true)
        ..flush();
      rig.drain();
      expect(rig.fake.batches.single.json, contains('"consent":"granted"'));
      final names = rig.fake.collector.names();
      expect(names.take(3), <String>['pulse_install', 'pulse_session_start', 'level_start']);
      expect(names.last, 'pulse_consent');
    });

    test('consent refused drops the buffered events and is remembered', () {
      rig.options.consentRequired = true;
      rig.start()
        ..levelStart(1)
        ..setConsent(false);
      rig.step(120000, 3);
      expect(rig.fake.requests, isEmpty);
      expect(rig.queueFiles(), isEmpty);
      expect(rig.restart().stats.optedOut, isTrue);
    });

    test('data deletion request is retried and the id rotates', () {
      var deletes = 0;
      rig.fake.responder =
          (s) => s.url.endsWith('/v2/delete') && deletes++ == 0 ? const PulseHttpResult(503) : FakeTransport.ok(s);
      final core = rig.start();
      final old = core.installId;
      core
        ..screen('menu')
        ..requestDataDeletion();
      expect(core.installId, isNot(old));
      rig.step(10000, 10);
      final dels = rig.fake.requests.where((r) => r.url.endsWith('/v2/delete')).toList();
      expect(dels.length, 2);
      expect(dels[1].json, contains('"install_id":"$old"'));
      core.flush();
      rig.drain();
      expect(rig.fake.collector.rows.every((r) => r['install_id'] != old), isTrue);
      expect(rig.fake.collector.named('pulse_install').length, 1);
    });

    test('reset install id keeps queued events under the old id', () {
      final core = rig.start();
      final a = core.installId;
      core
        ..screen('x')
        ..resetInstallId()
        ..screen('y')
        ..flush();
      rig.drain();
      final ids = rig.fake.collector.rows.map((r) => r['install_id']).toSet().toList();
      expect(ids.length, 2);
      expect(ids.first, a);
      expect(rig.fake.collector.seqs(core.installId).first, 1);
    });

    test('child mode strips uid and free text', () {
      rig.options.childDirected = true;
      final core = rig.start()
        ..setUserId('player-42')
        ..levelStart(1, levelName: 'Forest of Names')
        ..tutorialStep(2, stepName: 'drag the gem');
      core
        ..track('booster_used', PulseEventParams().addString('booster', 'hammer').addInt('count', 2))
        ..reportError('Exception', 'boom', 'A.b (x.dart:1)')
        ..flush();
      rig.drain();
      final json = rig.fake.batches.first.json;
      expect(json, contains('"child":true'));
      for (final secret in <String>['player-42', 'Forest', 'drag the gem', 'hammer', 'boom']) {
        expect(json, isNot(contains(secret)));
      }
      expect(json, contains('"count":2'));
    });

    test('a uid that looks like an e-mail is refused', () {
      rig.start()
        ..setUserId('someone@example.com')
        ..screen('x')
        ..flush();
      rig.drain();
      expect(rig.fake.batches.first.json, isNot(contains('example.com')));
    });
  });

  group('api', () {
    test('errors are scrubbed, deduped per session and capped at 10 per launch', () {
      final core = rig.start();
      for (var i = 0; i < 15; i++) {
        final t = 'E${String.fromCharCode(65 + i)}Exception';
        core
          ..reportError(t, 'user@mail.com opened /data/user/0/file.txt 42 times', 'Game.Run () (at Assets/Game.cs:$i)')
          ..reportError(t, 'user@mail.com opened /data/user/0/file.txt 43 times', 'Game.Run ()');
      }
      core.flush();
      rig.drain();
      final errs = rig.fake.collector.named('pulse_error').map(p).toList();
      expect(errs.length, 10);
      expect(errs[0]['msg'], '<email> opened <path> # times');
      expect(errs[0]['frame'], 'Game.Run');
      expect(errs[0]['fp'], PulseErrors.fingerprint('EAException', 'Game.Run', '<email> opened <path> # times'));
    });

    test('scrub, frame and fingerprint rules match the Unity SDK', () {
      expect(PulseErrors.scrub('Index 12 out of range 0x7ffe 9f86d081884c7d65 C:\\x\\y\nsecond line'),
          'Index # out of range <hex> <id> <path>');
      expect(PulseErrors.topFrame('Board.Swap () (at Assets/Board.cs:42)\nUnityEngine.Debug:Log'), 'Board.Swap');
      expect(PulseErrors.topFrame('#0      Board.swap (package:game/board.dart:42:7)'), 'Board.swap');
      expect(PulseErrors.topFrame('   at Game.Program.Main(String[] args) in /src/Program.cs:line 9'), 'Game.Program.Main');
      // FNV-1a 64 test vectors: "" and "a"
      expect(PulseErrors.fingerprint('', null, '').length, 16);
    });

    test('custom events are validated', () {
      final core = rig.start();
      expect(
          core.track('booster_used',
              PulseEventParams().addString('booster', 'hammer').addInt('level', 38).addDouble('power', 1.5).addBool('golden', true)),
          isTrue);
      expect(core.track('Booster Used'), isFalse);
      expect(core.track('session_start'), isFalse);
      expect(core.track('firebase_x'), isFalse);
      expect(core.track('pulse_custom'), isFalse);
      final many = PulseEventParams();
      for (var i = 0; i < 26; i++) {
        many.addInt('p$i', i);
      }
      expect(core.track('too_many', many), isFalse);
      expect(core.track('bad_param', PulseEventParams().addInt('Bad Key', 1)), isFalse);
      core
        ..track('long_text', PulseEventParams().addString('t', 'x' * 300))
        ..flush();
      rig.drain();
      expect(rig.fake.collector.named('booster_used').length, 1);
      expect((p(rig.fake.collector.named('long_text').single)['t']! as String).length, 100);
      expect(core.stats.rejected, 6);
    });

    test('enum parameters are checked', () {
      rig.start()
        ..settingChange('volume', '1', '0')
        ..settingToggle(PulseSetting.haptics, from: true, to: false)
        ..adEvent('explode', 'rewarded')
        ..levelQuit(1, 'rage')
        ..flush();
      rig.drain();
      final c = rig.fake.collector;
      expect(c.named('setting_change').length, 1);
      expect(p(c.named('setting_change').single)['to'], 'off');
      expect(c.named('ad_event'), isEmpty);
      expect(p(c.named('level_quit').single)['reason'], 'menu');
    });

    test('store mapping matches the Unity SDK', () {
      expect(PulseInstallStores.fromAndroidInstaller('com.android.vending'), 'google_play');
      expect(PulseInstallStores.fromAndroidInstaller(null), 'sideload');
      expect(PulseInstallStores.fromAndroidInstaller('com.some.store'), 'unknown');
      expect(PulseInstallStores.fromIos('sandbox'), 'testflight');
      expect(PulseInstallStores.fromIos('production'), 'app_store');
      expect(PulseInstallStores.fromIos('xcode'), 'sideload');
    });

    test('strings are escaped and cut like the Unity SDK', () {
      final w = PulseJsonWriter()..string('a"b\\c\n\t\u0001\u007f é 🎮', 100);
      expect(utf8.decode(w.toBytes()), '"a\\"b\\\\c\\n\\t\\u0001\u007f é 🎮"');
      final cut = PulseJsonWriter()..string('ab🎮', 3);
      expect(String.fromCharCodes(cut.toBytes()), '"ab"');
      final lone = PulseJsonWriter()..string('x\ud800y');
      expect(lone.toBytes(), <int>[0x22, 0x78, 0xEF, 0xBF, 0xBD, 0x79, 0x22]);
      expect(PulseCore.safeAppId('1game'), 'app.1game');
      expect(PulseCore.safeAppId('com.my game'), 'com.my_game');
    });
  });
}
