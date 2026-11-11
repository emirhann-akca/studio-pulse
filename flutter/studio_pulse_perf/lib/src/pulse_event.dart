import 'dart:convert';

/// One event of the contract: a name and up to 25 int / double / String parameters.
class PulseEvent {
  PulseEvent(this.name, this.timestamp, this.params);

  final String name;
  final DateTime timestamp;
  final Map<String, Object> params;

  /// The JSON-lines envelope read by the `unity_jsonl` connector (docs/EVENTS.md).
  String toJsonLine({
    required String app,
    required String appVersion,
    required String platform,
    required String session,
  }) {
    final envelope = <String, Object>{
      'v': 1,
      'event': name,
      'ts': isoMillis(timestamp),
      'app': app,
      'app_version': appVersion,
      'platform': platform,
      'session': session,
      'params': params,
    };
    return jsonEncode(envelope);
  }

  /// UTC ISO 8601 with milliseconds: 2026-10-01T12:00:30.000Z.
  static String isoMillis(DateTime t) {
    final u = t.toUtc();
    String two(int v) => v.toString().padLeft(2, '0');
    return '${u.year.toString().padLeft(4, '0')}-${two(u.month)}-${two(u.day)}'
        'T${two(u.hour)}:${two(u.minute)}:${two(u.second)}'
        '.${u.millisecond.toString().padLeft(3, '0')}Z';
  }
}
