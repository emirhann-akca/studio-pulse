import 'dart:async';
import 'dart:io';

import 'pulse_event.dart';

/// Where events go. A sink is called at most once per window (every 30 s by
/// default), never per frame.
abstract class PulseSink {
  void send(PulseEvent event);
  Future<void> flush() async {}
}

/// Firebase Analytics, without depending on the plugin: pass the logEvent
/// function of your FirebaseAnalytics instance:
///
///     FirebaseAnalyticsSink(FirebaseAnalytics.instance.logEvent)
///
/// The events then reach the GA4 BigQuery export, where the `ga4_bigquery`
/// source reads them with the same parameter names.
class FirebaseAnalyticsSink implements PulseSink {
  FirebaseAnalyticsSink(this.logEvent);

  final Future<void> Function({required String name, Map<String, Object>? parameters}) logEvent;

  @override
  void send(PulseEvent event) {
    unawaited(logEvent(name: event.name, parameters: event.params));
  }

  @override
  Future<void> flush() async {}
}

/// JSON lines appended to a file (collect the files and point the
/// `unity_jsonl` source at them).
class JsonLinesSink implements PulseSink {
  JsonLinesSink(
    this.file, {
    required this.app,
    required this.appVersion,
    required this.platform,
    required this.session,
  });

  final File file;
  final String app;
  final String appVersion;
  final String platform;
  final String session;
  final StringBuffer _pending = StringBuffer();

  @override
  void send(PulseEvent event) {
    _pending
      ..write(
          event.toJsonLine(app: app, appVersion: appVersion, platform: platform, session: session))
      ..write('\n');
  }

  @override
  Future<void> flush() async {
    if (_pending.isEmpty) return;
    final text = _pending.toString();
    _pending.clear();
    await file.parent.create(recursive: true);
    await file.writeAsString(text, mode: FileMode.append, flush: true);
  }
}

/// Keeps events in memory (tests, debugging).
class MemorySink implements PulseSink {
  final List<PulseEvent> events = <PulseEvent>[];

  @override
  void send(PulseEvent event) => events.add(event);

  @override
  Future<void> flush() async {}
}
