// E2E driver: a realistic play session through the Dart core (no Flutter needed) against
// a running `pulse collect`, with the real clock and dart:io HTTP. Prints a JSON summary.
//   dart run tool/e2e_driver.dart <collector url> <write key> <data dir> [platform] [store] [app id]
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:studio_pulse/core.dart';

Future<void> main(List<String> args) async {
  if (args.length < 3) {
    stderr.writeln('usage: <url> <write key> <data dir> [platform] [store] [app id]');
    exitCode = 2;
    return;
  }
  final platform = args.length > 3 ? args[3] : 'ios';
  final store = args.length > 4 ? args[4] : 'app_store';
  final appId = args.length > 5 ? args[5] : 'com.studio.e2e.flutter';
  final logs = <String>[];
  final transport = IoHttpTransport(timeout: const Duration(seconds: 10));
  final core = PulseCore(
    args[1].isEmpty ? 'sp_pub_local' : args[1],
    args[0],
    PulseOptions(),
    PulseDevice(
      appId: appId,
      platform: platform,
      appVersion: '2.3.0',
      build: '230',
      buildEnv: 'release',
      installStore: store,
      installerRaw: store == 'google_play' ? 'com.android.vending' : null,
      appName: 'E2E Flutter App',
      sdk: PulseCore.sdkName,
      engine: 'flutter',
      os: platform == 'ios' ? 'ios 18.1' : 'android 15 / api-35',
      deviceModel: platform == 'ios' ? 'iPhone16,1' : 'Pixel 8',
      deviceTier: 'high',
      ramMb: 8192,
      locale: 'tr-TR',
      tzOffsetMin: 180,
    ),
    PulseEnvironment(dataDirectory: args[2], transport: transport, log: logs.add),
  )..start();

  core
    ..tutorialBegin()
    ..tutorialStep(1, stepName: 'tap to swap')
    ..tutorialComplete()
    ..screen('menu', screenClass: 'MenuScreen')
    ..settingChange(PulseSetting.music, 'on', 'off')
    ..settingToggle(PulseSetting.haptics, from: true, to: false);
  for (var level = 1; level <= 5; level++) {
    core.levelStart(level, world: 'forest');
    await Future<void>.delayed(const Duration(milliseconds: 5));
    if (level >= 3) {
      core
        ..levelFail(level, 'out_of_time', posX: 0.2 + 0.1 * level, posY: 0.4, progress: 0.6)
        ..levelStart(level, world: 'forest');
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }
    if (level == 5) {
      core.levelQuit(level, PulseQuitReason.menu, progress: 0.3, posX: 0.5, posY: 0.5);
      break;
    }
    core
      ..levelComplete(level, score: 1000 * level, stars: 3)
      ..earnCurrency('gems', 5, itemName: 'level_reward', level: level);
  }
  core
    ..spendCurrency('gems', 12, itemName: 'skin_fox')
    ..adEvent(PulseAdAction.show, 'interstitial', placement: 'level_end')
    ..adImpression('levelplay', 'interstitial', 0.0042, 'USD', adSource: 'unity_ads', placement: 'level_end', precision: 'estimated')
    ..purchase('e2e-dart-${DateTime.now().microsecondsSinceEpoch}', 'no_ads', 89990000, 'TRY', isRestore: false, isSandbox: false);
  try {
    throw StateError('Bad state at /data/user/0/app 7');
  } on StateError catch (e, st) {
    core.reportError(e.runtimeType.toString(), e.toString(), st.toString());
  }
  core
    ..track('booster_used', PulseEventParams().addString('booster', 'shuffle').addInt('level', 4))
    ..onBackground()
    ..onForeground()
    ..flush();

  final until = DateTime.now().add(const Duration(seconds: 30));
  while (DateTime.now().isBefore(until)) {
    await Future<void>.delayed(const Duration(milliseconds: 20));
    core
      ..tick()
      ..flush();
    final s = core.stats;
    if (s.queued == 0 && s.sent >= s.recorded) break;
  }
  core
    ..onQuit()
    ..close();
  transport.close();
  final s = core.stats;
  stdout.writeln(jsonEncode(<String, Object?>{
    'sdk': PulseCore.sdkName,
    'install_id': core.installId,
    'recorded': s.recorded,
    'sent': s.sent,
    'queued': s.queued,
    'rejected': s.rejected,
    'last_status': s.lastStatus,
    if (s.lastError != null) 'last_error': s.lastError,
  }));
  exitCode = s.queued == 0 && s.sent >= s.recorded ? 0 : 1; // a run also sends what earlier runs left
}
