import 'dart:async';
import 'dart:io';
import 'dart:ui' show AppExitResponse, AppLifecycleState, PlatformDispatcher;

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';

import '../contract.g.dart';
import '../core/core.dart';
import '../core/environment.dart';
import '../core/options.dart';
import '../core/text.dart';
import '../device_tier.dart';
import '../pulse_event.dart';
import '../pulse_perf.dart';
import '../sinks.dart';

/// The Flutter SDK facade (docs/SDK_FLUTTER.md):
///
///     WidgetsFlutterBinding.ensureInitialized();
///     await StudioPulse.init(writeKey: 'sp_pub_...', endpoint: 'https://collector.example.com');
///     StudioPulse.levelStart(12);
///
/// Hooks AppLifecycleListener (background / foreground / exit), FlutterError.onError and
/// PlatformDispatcher.instance.onError (previous handlers, e.g. Crashlytics, still run),
/// ticks the core once a second and, unless `perf: false`, starts [PulsePerf] with its
/// pulse_perf / pulse_load events going through the same queue. Calls before [init] are
/// no-ops; nothing throws into the app.
class StudioPulse {
  StudioPulse._();

  static const MethodChannel _channel = MethodChannel('studio_pulse');
  static PulseCore? _core;
  static AppLifecycleListener? _lifecycle;
  static Timer? _timer;
  static PulsePerf? _perf;

  /// The SDK core (null before [init]): for tests and advanced use.
  static PulseCore? get core => _core;

  /// The perf sampler started by [init] (null with `perf: false`); set `scene` per screen.
  static PulsePerf? get perf => _perf;

  static Future<void> init({
    required String writeKey,
    required String endpoint,
    PulseOptions? options,
    bool perf = true,
    DateTime? processStart,
  }) async {
    if (_core != null) return;
    try {
      final opts = options ?? PulseOptions();
      final info = await _platformInfo();
      final device = deviceFrom(info);
      final files = info['files_dir'] as String? ?? '${Directory.systemTemp.path}${Platform.pathSeparator}${device.appId}';
      final env = PulseEnvironment(
        dataDirectory: '$files${Platform.pathSeparator}studio-pulse',
        noBackupDirectory: info['no_backup_dir'] as String?,
        excludeFromBackup: Platform.isIOS ? (path) => unawaited(_channel.invokeMethod<void>('excludeFromBackup', path)) : null,
        previousExit: () => exitFrom(info),
        transport: IoHttpTransport(),
        log: kReleaseMode ? null : debugPrint,
      );
      final core = PulseCore(writeKey, endpoint, opts, device, env)..start();
      _core = core;
      if (Platform.isIOS) {
        // AppTransaction may need the network: never hold app start for it
        unawaited(_channel.invokeMethod<String>('appTransaction').then((env) {
          if (env != null) core.setInstallSource(PulseInstallStores.fromIos(env), null);
        }, onError: (Object _) {}));
      }
      _lifecycle = AppLifecycleListener(
        onStateChange: _onState,
        onExitRequested: () async {
          core.onQuit();
          return AppExitResponse.exit;
        },
      );
      _timer = Timer.periodic(const Duration(seconds: 1), (_) => core.tick());
      if (opts.captureErrors) _hookErrors(core);
      if (perf) {
        _perf = PulsePerf(
          sinks: <PulseSink>[StudioPulseSink(core)],
          processStart: processStart,
          deviceMemoryMb: device.ramMb > 0 ? device.ramMb : null,
        )..start();
      }
    } on Object catch (e) {
      debugPrint('[studio_pulse] init failed: $e');
    }
  }

  static void _onState(AppLifecycleState s) {
    final core = _core;
    if (core == null) return;
    switch (s) {
      case AppLifecycleState.resumed:
        core.onForeground();
      case AppLifecycleState.paused:
      case AppLifecycleState.hidden:
        unawaited(_perf?.flush());
        core.onBackground();
      case AppLifecycleState.detached:
        core.onQuit();
      case AppLifecycleState.inactive:
        break;
    }
  }

  static void _hookErrors(PulseCore core) {
    final previous = FlutterError.onError;
    FlutterError.onError = (FlutterErrorDetails d) {
      core.reportError(d.exception.runtimeType.toString(), d.exceptionAsString(), d.stack?.toString());
      if (previous != null) {
        previous(d);
      } else {
        FlutterError.presentError(d);
      }
    };
    final dispatcher = PlatformDispatcher.instance;
    final previousDispatcher = dispatcher.onError;
    dispatcher.onError = (Object error, StackTrace stack) {
      core.reportError(error.runtimeType.toString(), error.toString(), stack.toString());
      // false keeps Flutter's default report when nobody else handled it
      return previousDispatcher?.call(error, stack) ?? false;
    };
  }

  static Future<Map<String, Object?>> _platformInfo() async {
    try {
      return await _channel.invokeMapMethod<String, Object?>('info') ?? <String, Object?>{};
    } on MissingPluginException {
      return <String, Object?>{}; // desktop and tests: no native side
    } on PlatformException {
      return <String, Object?>{};
    }
  }

  /// Device context from the platform channel's answer (visible for tests).
  @visibleForTesting
  static PulseDevice deviceFrom(Map<String, Object?> info, {String? operatingSystem}) {
    final osName = operatingSystem ?? Platform.operatingSystem;
    final platform = switch (osName) {
      'android' => PulsePlatform.android,
      'ios' => PulsePlatform.ios,
      'macos' => PulsePlatform.macos,
      'windows' => PulsePlatform.windows,
      _ => PulsePlatform.linux,
    };
    String store;
    String? raw;
    if (osName == 'android') {
      raw = info['installer'] as String?;
      store = PulseInstallStores.fromAndroidInstaller(raw);
    } else if (osName == 'ios') {
      store = PulseInstallStores.fromIos(info['install_source'] as String?);
    } else {
      store = PulseInstallStore.unknown;
    }
    final ram = info['ram_mb'] is int ? info['ram_mb']! as int : -1;
    final os = (info['os'] as String?) ?? '${Platform.operatingSystem} ${Platform.operatingSystemVersion}';
    return PulseDevice(
      appId: (info['app_id'] as String?) ?? 'unknown.app',
      platform: platform,
      appVersion: (info['app_version'] as String?) ?? '0',
      build: info['build'] as String?,
      buildEnv: kReleaseMode ? PulseBuildEnv.release : PulseBuildEnv.debug,
      installStore: store,
      installerRaw: raw,
      appName: info['app_name'] as String?,
      sdk: PulseCore.sdkName,
      engine: 'flutter',
      os: os.length > 50 ? os.substring(0, 50) : os,
      deviceModel: info['device_model'] as String?,
      deviceTier: DeviceTier.classify(memoryMb: ram > 0 ? ram : null, processors: Platform.numberOfProcessors),
      ramMb: ram,
      locale: PlatformDispatcher.instance.locale.toLanguageTag(),
      tzOffsetMin: DateTime.now().timeZoneOffset.inMinutes,
    );
  }

  /// Android ApplicationExitInfo.REASON_* (from the channel) -> contract exit_reason.
  @visibleForTesting
  static PulseExitInfo? exitFrom(Map<String, Object?> info) {
    final r = info['exit_reason'];
    if (r is! int) return null;
    final String reason;
    switch (r) {
      case 3:
        reason = PulseExitReason.lowMemory;
      case 4:
        reason = PulseExitReason.crash;
      case 5:
        reason = PulseExitReason.crashNative;
      case 6:
        reason = PulseExitReason.anr;
      case 1:
      case 10:
      case 11:
        reason = PulseExitReason.user;
      default:
        reason = PulseExitReason.unknown;
    }
    final ts = info['exit_ts'];
    final rss = info['exit_rss_kb'];
    return PulseExitInfo(reason, timestampMs: ts is int ? ts : 0, rssKb: rss is int ? rss : -1);
  }

  // ---------------------------------------------------------------- game and app events

  static void levelStart(int level, {String? levelName, String? world, String? mode}) =>
      _core?.levelStart(level, levelName: levelName, world: world, mode: mode);

  static void levelComplete(int level, {int? score, int? stars, int? movesLeft}) =>
      _core?.levelComplete(level, score: score, stars: stars, movesLeft: movesLeft);

  /// [x]/[y]: where the player failed, 0-1 of the level bounds (or world units with
  /// `worldSpace: true`); [progress]: share of the level done, 0-1.
  static void levelFail(int level, String reason, {double? x, double? y, double? progress, bool worldSpace = false, int? score}) =>
      _core?.levelFail(level, reason,
          posX: x, posY: y, progress: progress, posSpace: worldSpace ? PulsePosSpace.world : null, score: score);

  static void levelQuit(int level, {String reason = PulseQuitReason.menu, double? progress, double? x, double? y}) =>
      _core?.levelQuit(level, reason, progress: progress, posX: x, posY: y);

  static void tutorialBegin([String? tutorial]) => _core?.tutorialBegin(tutorial);

  static void tutorialStep(int step, {String? stepName, String? tutorial}) =>
      _core?.tutorialStep(step, stepName: stepName, tutorial: tutorial);

  static void tutorialComplete([String? tutorial]) => _core?.tutorialComplete(tutorial);

  /// [setting]: PulseSetting.music, sfx, haptics, notifications, language, quality.
  static void settingChange(String setting, String? from, String to) => _core?.settingChange(setting, from, to);

  static void settingToggle(String setting, {required bool from, required bool to}) =>
      _core?.settingToggle(setting, from: from, to: to);

  static void earnCurrency(String currency, double amount, {String? item, int? level}) =>
      _core?.earnCurrency(currency, amount, itemName: item, level: level);

  static void spendCurrency(String currency, double amount, {String? item, int? level}) =>
      _core?.spendCurrency(currency, amount, itemName: item, level: level);

  /// [priceMicros]: price paid in micros (4.99 = 4990000).
  static void purchase(String transactionId, String productId, int priceMicros, String currency,
          {bool? isRestore, bool? isSandbox}) =>
      _core?.purchase(transactionId, productId, priceMicros, currency, isRestore: isRestore, isSandbox: isSandbox);

  static void adImpression(String adPlatform, String adFormat, double value,
          {String currency = 'USD', String? adSource, String? adUnitName, String? placement, String? precision}) =>
      _core?.adImpression(adPlatform, adFormat, value, currency,
          adSource: adSource, adUnitName: adUnitName, placement: placement, precision: precision);

  static void adEvent(String action, String adFormat, {String? placement, String? errorCode}) =>
      _core?.adEvent(action, adFormat, placement: placement, errorCode: errorCode);

  static void screen(String name, {String? screenClass}) => _core?.screen(name, screenClass: screenClass);

  /// A custom event; values must be int, double, bool or String (at most 25).
  static bool track(String name, [Map<String, Object?> params = const <String, Object?>{}]) =>
      _core?.track(name, PulseEventParams.fromMap(params)) ?? false;

  static void reportError(Object error, StackTrace? stack, {bool fatal = false}) =>
      _core?.reportError(error.runtimeType.toString(), error.toString(), stack?.toString(), fatal: fatal);

  // ---------------------------------------------------------------- privacy

  static void setUserId(String? gameAccountId) => _core?.setUserId(gameAccountId);
  static void setConsent(bool granted) => _core?.setConsent(granted);
  static void optOut() => _core?.optOut();
  static void requestDataDeletion() => _core?.requestDataDeletion();
  static void resetInstallId() => _core?.resetInstallId();
  static String? get installId => _core?.installId;
  static PulseStats? get stats => _core?.stats;
  static void flush() => _core?.flush();

  /// Stops timers and hooks (tests; apps keep the SDK for their lifetime).
  @visibleForTesting
  static void shutdown() {
    _timer?.cancel();
    _lifecycle?.dispose();
    _perf?.stop();
    _core?.close();
    _timer = null;
    _lifecycle = null;
    _perf = null;
    _core = null;
  }
}

/// Forwards PulsePerf's pulse_perf / pulse_load events into the SDK queue.
class StudioPulseSink implements PulseSink {
  StudioPulseSink(this._core);

  final PulseCore _core;
  final PulseEventParams _p = PulseEventParams();

  @override
  void send(PulseEvent event) {
    _p.clear();
    event.params.forEach((k, v) {
      if (v is int) {
        _p.addInt(k, v);
      } else if (v is double) {
        _p.addDouble(k, v);
      } else if (v is String) {
        _p.addString(k, v);
      } else if (v is bool) {
        _p.addBool(k, v);
      }
    });
    _core.recordPerf(event.name, _p);
  }

  @override
  Future<void> flush() async {}
}
