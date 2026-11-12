// The Flutter facade: platform channel answers, AppLifecycleListener, the error hooks
// (chained to the previous handlers) and delivery to a real in-process HTTP server.
import 'dart:io';
import 'dart:ui' show AppLifecycleState, PlatformDispatcher;

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:studio_pulse/studio_pulse.dart';

import 'support/kit.dart';

Map<String, Object?> p(Map<String, Object?> row) => row['p']! as Map<String, Object?>;

void main() {
  final binding = TestWidgetsFlutterBinding.ensureInitialized();
  HttpOverrides.global = null; // flutter_test blocks real HTTP by default; this test uses 127.0.0.1

  test('device context from the Android and iOS channel answers', () {
    final android = StudioPulse.deviceFrom(<String, Object?>{
      'app_id': 'com.example.flutter',
      'app_version': '3.1.0',
      'build': '31',
      'installer': 'com.android.vending',
      'ram_mb': 2048,
      'device_model': 'SM-A145F',
      'os': 'android 14 / api-34',
    }, operatingSystem: 'android');
    expect(android.platform, 'android');
    expect(android.installStore, 'google_play');
    expect(android.installerRaw, 'com.android.vending');
    expect(android.deviceTier, 'low');
    expect(android.sdk, 'flutter/2.0.0');
    final sideload = StudioPulse.deviceFrom(<String, Object?>{'installer': null}, operatingSystem: 'android');
    expect(sideload.installStore, 'sideload');
    final tf = StudioPulse.deviceFrom(<String, Object?>{'install_source': 'sandbox'}, operatingSystem: 'ios');
    expect(tf.installStore, 'testflight');
    expect(StudioPulse.deviceFrom(<String, Object?>{'install_source': 'production'}, operatingSystem: 'ios').installStore,
        'app_store');
    expect(StudioPulse.exitFrom(<String, Object?>{'exit_reason': 6, 'exit_ts': 5, 'exit_rss_kb': 1000})!.reason, 'anr');
    expect(StudioPulse.exitFrom(<String, Object?>{'exit_reason': 5})!.reason, 'crash_native');
    expect(StudioPulse.exitFrom(<String, Object?>{}), isNull);
  });

  test('init, lifecycle and error hooks deliver to a real collector', () async {
    final server = await TestServer.start();
    final tmp = Directory.systemTemp.createTempSync('pulse-flutter-');
    binding.defaultBinaryMessenger.setMockMethodCallHandler(const MethodChannel('studio_pulse'), (call) async {
      if (call.method == 'info') {
        return <String, Object?>{
          'app_id': 'com.example.flutter',
          'app_version': '3.1.0',
          'build': '31',
          'app_name': 'Example Flutter',
          'files_dir': tmp.path,
          'no_backup_dir': '${tmp.path}${Platform.pathSeparator}nobackup',
          'ram_mb': 4096,
          'device_model': 'Pixel 8',
          'os': 'android 15 / api-35',
        };
      }
      return null;
    });
    final originalFlutterError = FlutterError.onError;
    final originalDispatcher = PlatformDispatcher.instance.onError;
    var previousCalled = 0;
    FlutterError.onError = (d) => previousCalled++; // stands in for Crashlytics
    try {
      await StudioPulse.init(writeKey: 'sp_pub_local', endpoint: server.url, perf: false);
      expect(StudioPulse.core, isNotNull);
      StudioPulse.levelStart(1);
      StudioPulse.levelFail(1, 'out_of_moves', x: 0.5, y: 0.25, progress: 0.4);
      StudioPulse.settingToggle(PulseSetting.music, from: true, to: false);
      StudioPulse.track('booster_used', <String, Object?>{'booster': 'hammer', 'level': 1});
      FlutterError.reportError(FlutterErrorDetails(exception: StateError('Bad state 42'), stack: StackTrace.current));
      expect(previousCalled, 1, reason: 'the previous FlutterError.onError still runs');
      PlatformDispatcher.instance.onError!(ArgumentError('async boom'), StackTrace.current);
      for (final s in <AppLifecycleState>[AppLifecycleState.inactive, AppLifecycleState.hidden, AppLifecycleState.paused]) {
        binding.handleAppLifecycleStateChanged(s);
      }
      for (var i = 0; i < 300 && server.collector.named('pulse_background').isEmpty; i++) {
        await Future<void>.delayed(const Duration(milliseconds: 10));
        StudioPulse.core!.tick();
      }
      for (final s in <AppLifecycleState>[AppLifecycleState.hidden, AppLifecycleState.inactive, AppLifecycleState.resumed]) {
        binding.handleAppLifecycleStateChanged(s);
      }
      StudioPulse.flush();
      for (var i = 0; i < 300 && server.collector.named('pulse_foreground').isEmpty; i++) {
        await Future<void>.delayed(const Duration(milliseconds: 10));
        StudioPulse.core!.tick();
        StudioPulse.flush();
      }
      final c = server.collector;
      expect(c.names(), containsAll(<String>[
        'pulse_install', 'pulse_session_start', 'level_start', 'level_fail', 'setting_change',
        'booster_used', 'pulse_error', 'pulse_background', 'pulse_foreground',
      ]));
      expect(c.named('pulse_error').length, 2);
      expect(p(c.named('pulse_error').first)['msg'], 'Bad state: Bad state #');
      expect(p(c.named('level_fail').single)['pos_x'], 0.5);
      final body = server.requests.firstWhere((r) => r.url.endsWith('/v2/batch')).body;
      final ctx = body['ctx']! as Map<String, Object?>;
      expect(ctx['app_id'], 'com.example.flutter');
      expect(ctx['sdk'], 'flutter/2.0.0');
      expect(ctx['build_env'], 'debug');
      expect(c.seqs(), <int>[for (var i = 1; i <= c.rows.length; i++) i]);
      expect(File('${tmp.path}${Platform.pathSeparator}nobackup${Platform.pathSeparator}studio-pulse.install').existsSync(),
          isTrue);
    } finally {
      StudioPulse.shutdown();
      FlutterError.onError = originalFlutterError;
      PlatformDispatcher.instance.onError = originalDispatcher;
      binding.defaultBinaryMessenger.setMockMethodCallHandler(const MethodChannel('studio_pulse'), null);
      await server.close();
      try {
        tmp.deleteSync(recursive: true);
      } on Object {
        // best effort
      }
    }
  });
}
