import '../contract.g.dart';

/// SDK behaviour; defaults from event contract v2 (PulseLimits).
class PulseOptions {
  PulseOptions({
    this.consentRequired = false,
    this.childDirected = false,
    this.captureErrors = true,
    this.flushEvents = PulseLimits.flushEvents,
    this.flushIntervalMs = PulseLimits.flushIntervalMs,
    this.sessionTimeoutMs = PulseLimits.sessionTimeoutMs,
    this.sessionMaxMs = PulseLimits.sessionMaxMs,
    this.heartbeatMs = PulseLimits.heartbeatMs,
    this.queueMaxEvents = PulseLimits.queueMaxEvents,
    this.queueMaxBytes = PulseLimits.queueMaxBytes,
    this.errorsPerLaunch = PulseLimits.errorsPerLaunch,
    this.retryMinMs = PulseLimits.retryMinMs,
    this.retryMaxMs = PulseLimits.retryMaxMs,
    this.retryJitterMinMs = 1000,
    this.retryJitterMaxMs = 5000,
    this.maxBatchEvents = 100,
    this.maxBatchBytes = 900 * 1024,
    this.gzipMinBytes = 1024,
    this.segmentMaxEvents = 100,
    this.segmentMaxBytes = 256 * 1024,
    this.pendingConsentEvents = 500,
  });

  /// Nothing is written to disk or sent until setConsent(true); earlier events wait in memory.
  bool consentRequired;

  /// Child-directed: no uid, no free-text parameters, ctx.child = true.
  bool childDirected;

  /// Capture FlutterError.onError / PlatformDispatcher.onError errors as pulse_error.
  bool captureErrors;

  int flushEvents;
  int flushIntervalMs;
  int sessionTimeoutMs;
  int sessionMaxMs;
  int heartbeatMs;
  int queueMaxEvents;
  int queueMaxBytes;
  int errorsPerLaunch;
  int retryMinMs;
  int retryMaxMs;
  int retryJitterMinMs;
  int retryJitterMaxMs;

  /// Events per request; a 413 halves it for this launch.
  int maxBatchEvents;
  int maxBatchBytes;
  int gzipMinBytes;
  int segmentMaxEvents;
  int segmentMaxBytes;
  int pendingConsentEvents;
}

enum PulseValueKind { integer, float, text, flag }

/// Parameters of a custom event: at most 25 int / double / bool / String values.
class PulseEventParams {
  final List<String> keys = <String>[];
  final List<PulseValueKind> kinds = <PulseValueKind>[];
  final List<Object> values = <Object>[];
  bool overflow = false;

  PulseEventParams clear() {
    keys.clear();
    kinds.clear();
    values.clear();
    overflow = false;
    return this;
  }

  int get length => keys.length;

  PulseEventParams _put(String key, PulseValueKind kind, Object value) {
    final i = keys.indexOf(key);
    if (i >= 0) {
      kinds[i] = kind;
      values[i] = value;
    } else if (keys.length >= PulseLimits.maxParams) {
      overflow = true;
    } else {
      keys.add(key);
      kinds.add(kind);
      values.add(value);
    }
    return this;
  }

  PulseEventParams addInt(String key, int value) => _put(key, PulseValueKind.integer, value);
  PulseEventParams addDouble(String key, double value) => _put(key, PulseValueKind.float, value);
  PulseEventParams addBool(String key, bool value) => _put(key, PulseValueKind.flag, value);
  PulseEventParams addString(String key, String? value) =>
      value == null ? this : _put(key, PulseValueKind.text, value);

  /// From a map: int, double, bool and String values (others are refused).
  static PulseEventParams fromMap(Map<String, Object?> map) {
    final p = PulseEventParams();
    map.forEach((k, v) {
      if (v is int) {
        p.addInt(k, v);
      } else if (v is double) {
        p.addDouble(k, v);
      } else if (v is bool) {
        p.addBool(k, v);
      } else if (v is String) {
        p.addString(k, v);
      } else {
        p.overflow = true;
      }
    });
    return p;
  }
}

/// Counters for diagnostics and tests (local only).
class PulseStats {
  const PulseStats({
    required this.queued,
    required this.recorded,
    required this.sent,
    required this.dropped,
    required this.rejected,
    required this.requests,
    required this.lastStatus,
    required this.lastError,
    required this.disabled,
    required this.optedOut,
    required this.consentState,
  });

  /// Events waiting on disk (or in memory before consent).
  final int queued;
  final int recorded;
  final int sent;

  /// Events deleted by the queue cap (oldest first).
  final int dropped;

  /// Refused locally, or in a batch the collector answered 400.
  final int rejected;
  final int requests;
  final int lastStatus;
  final String? lastError;

  /// True after a 401/403: nothing is sent until the next launch.
  final bool disabled;
  final bool optedOut;
  final String consentState;
}
