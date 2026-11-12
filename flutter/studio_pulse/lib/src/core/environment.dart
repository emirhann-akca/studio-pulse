import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import '../contract.g.dart';

/// Device wall clock (timestamps) and a monotonic clock (durations, timers).
abstract class PulseClock {
  int nowMs();
  int monotonicMs();
}

class SystemClock implements PulseClock {
  final Stopwatch _sw = Stopwatch()..start();

  @override
  int nowMs() => DateTime.now().millisecondsSinceEpoch;

  @override
  int monotonicMs() => _sw.elapsedMilliseconds;
}

abstract class PulseRandom {
  void nextBytes(Uint8List buffer, int offset, int count);
}

/// Random.secure(): install, session and event ids, retry jitter.
class CryptoRandom implements PulseRandom {
  final Random _r = Random.secure();

  @override
  void nextBytes(Uint8List buffer, int offset, int count) {
    for (var i = 0; i < count; i++) {
      buffer[offset + i] = _r.nextInt(256);
    }
  }
}

/// SplitMix64, byte for byte the Unity SDK's SeededRandom: the conformance test runs
/// both SDKs with one seed and expects identical ids. Native platforms only (64-bit
/// ints); not for production ids.
class SeededRandom implements PulseRandom {
  SeededRandom(int seed) : _state = seed;

  int _state;

  int nextUInt64() {
    _state += 0x9E3779B97F4A7C15;
    var z = _state;
    z = (z ^ (z >>> 30)) * 0xBF58476D1CE4E5B9;
    z = (z ^ (z >>> 27)) * 0x94D049BB133111EB;
    return z ^ (z >>> 31);
  }

  /// Each 8 bytes come from one draw, low byte first; a partial tail uses one more draw.
  @override
  void nextBytes(Uint8List buffer, int offset, int count) {
    var o = offset;
    var left = count;
    while (left > 0) {
      final v = nextUInt64();
      for (var i = 0; i < 8 && left > 0; i++, left--) {
        buffer[o++] = (v >>> (8 * i)) & 0xFF;
      }
    }
  }
}

/// Outcome of one HTTP request. status 0 = no HTTP answer (network error, timeout).
class PulseHttpResult {
  const PulseHttpResult(this.status, {this.retryAfterSeconds = -1, this.body, this.error});

  final int status;
  final int retryAfterSeconds;
  final String? body;
  final String? error;
}

abstract class PulseTransport {
  /// POST [body] to [url] (Content-Type application/json, X-Pulse-Key and, when [gzip],
  /// Content-Encoding: gzip) and call [done] exactly once.
  void post(String url, String writeKey, Uint8List body, bool gzip, void Function(PulseHttpResult) done);
}

/// dart:io HttpClient (Android, iOS, desktop; not the web).
class IoHttpTransport implements PulseTransport {
  IoHttpTransport({Duration timeout = const Duration(seconds: 20)})
      : _timeout = timeout,
        _client = HttpClient()..connectionTimeout = timeout;

  final Duration _timeout;
  final HttpClient _client;

  @override
  void post(String url, String writeKey, Uint8List body, bool gzip, void Function(PulseHttpResult) done) {
    unawaited(_post(url, writeKey, body, gzip).then(done));
  }

  Future<PulseHttpResult> _post(String url, String writeKey, Uint8List body, bool gzip) async {
    try {
      final req = await _client.postUrl(Uri.parse(url)).timeout(_timeout);
      req.headers.set(HttpHeaders.contentTypeHeader, 'application/json');
      req.headers.set('X-Pulse-Key', writeKey);
      if (gzip) req.headers.set(HttpHeaders.contentEncodingHeader, 'gzip');
      req.contentLength = body.length;
      req.add(body);
      final resp = await req.close().timeout(_timeout);
      final text = await utf8.decodeStream(resp).timeout(_timeout);
      final ra = int.tryParse(resp.headers.value('retry-after') ?? '');
      return PulseHttpResult(resp.statusCode, retryAfterSeconds: ra ?? -1, body: text);
    } on Object catch (e) {
      return PulseHttpResult(0, error: e.toString());
    }
  }

  void close() => _client.close(force: true);
}

/// What the host app and device are; the adapter fills it once per launch.
class PulseDevice {
  PulseDevice({
    this.appId = '',
    this.platform = PulsePlatform.editor,
    this.appVersion = '0',
    this.build,
    this.buildEnv = PulseBuildEnv.release,
    this.installStore = PulseInstallStore.unknown,
    this.installerRaw,
    this.appName,
    this.sdk,
    this.engine,
    this.os,
    this.deviceModel,
    this.deviceTier,
    this.ramMb = -1,
    this.locale,
    this.tzOffsetMin,
  });

  String appId;
  String platform;
  String appVersion;
  String? build;
  String buildEnv;
  String installStore;
  String? installerRaw;
  String? appName;
  String? sdk;
  String? engine;
  String? os;
  String? deviceModel;
  String? deviceTier;
  int ramMb;
  String? locale;
  int? tzOffsetMin;
}

/// The reason the previous process ended, as the OS reported it (Android ApplicationExitInfo).
class PulseExitInfo {
  const PulseExitInfo(this.reason, {this.timestampMs = 0, this.rssKb = -1});

  final String reason;
  final int timestampMs;
  final int rssKb;
}

/// Platform services the core calls. Only [dataDirectory] is required.
class PulseEnvironment {
  PulseEnvironment({
    required this.dataDirectory,
    this.noBackupDirectory,
    this.excludeFromBackup,
    this.previousExit,
    PulseClock? clock,
    PulseRandom? random,
    this.transport,
    this.log,
  })  : clock = clock ?? SystemClock(),
        random = random ?? CryptoRandom();

  /// State file and event queue (created when missing).
  final String dataDirectory;

  /// Never restored from a backup (Android getNoBackupFilesDir()); null: no restore check.
  final String? noBackupDirectory;

  /// Excludes a file from device backups (iOS); optional.
  final void Function(String path)? excludeFromBackup;

  /// The OS-reported reason the previous process ended, or null.
  final PulseExitInfo? Function()? previousExit;

  final PulseClock clock;
  final PulseRandom random;
  final PulseTransport? transport;
  final void Function(String message)? log;
}
