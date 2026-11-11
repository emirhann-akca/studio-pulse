import 'dart:io';
import 'dart:ui' show FrameTiming;

import 'package:flutter_test/flutter_test.dart';
import 'package:studio_pulse_perf/studio_pulse_perf.dart';

/// 1800 synthetic frames (30 s at 60 Hz): totals 16.0-17.8 ms in 0.2 ms steps
/// (i % 10), every 300th frame 40 ms. Build = 0.4 x total, raster = 0.5 x total.
///
/// By hand: p50 = 17.0 ms (ranks 899/900), p95 = 17.8 ms (ranks 1709/1710),
/// max 40 ms, slow (> 33.3 ms) = 6, janky (> 16.667 ms) = 1080 + 6 = 1086,
/// build p95 = 7.12 ms, raster p95 = 8.9 ms.
List<FrameTiming> frames(int from, int to, int baseUs, DateTime processStart) {
  final out = <FrameTiming>[];
  for (var i = from; i < to; i++) {
    final total = i % 300 == 0 ? 40000 : 16000 + (i % 10) * 200;
    final build = total * 2 ~/ 5;
    final raster = total ~/ 2;
    final vsync = baseUs + i * 16667;
    out.add(FrameTiming(
      vsyncStart: vsync,
      buildStart: vsync + 500,
      buildFinish: vsync + 500 + build,
      rasterStart: vsync + total - raster,
      rasterFinish: vsync + total,
      rasterFinishWallTime: processStart.microsecondsSinceEpoch + 2345678 + i * 16667,
    ));
  }
  return out;
}

void main() {
  final processStart = DateTime.utc(2026, 10, 1, 12, 0, 0);
  final wall = DateTime.utc(2026, 10, 1, 12, 0, 30);

  Future<(MemorySink, String)> run(Directory dir) async {
    var now = 0;
    final rss = [500, 600, 550, 580];
    var rssCall = 0;
    final memory = MemorySink();
    final file = File('${dir.path}/pulse.jsonl');
    final lines = JsonLinesSink(file,
        app: 'com.example.flutter', appVersion: '2.0.0', platform: 'ANDROID', session: 'f' * 32);
    final perf = PulsePerf(
      sinks: [memory, lines],
      processStart: processStart,
      deviceMemoryMb: 3072,
      processors: 8,
      clockMicros: () => now,
      wallClock: () => wall,
      rssBytes: () => rss[rssCall++ % rss.length] * 1024 * 1024,
    )..scene = 'Level';
    for (var b = 0; b < 18; b++) {
      now = b * 1666700;
      perf.onTimings(frames(b * 100, b * 100 + 100, 1000000, processStart));
    }
    now = 30000000;
    await perf.flush();
    return (memory, await file.readAsString());
  }

  test('one pulse_perf window and one cold start, numbers by hand', () async {
    final dir = await Directory.systemTemp.createTemp('pulse');
    final (memory, _) = await run(dir);
    expect(memory.events.map((e) => e.name), ['pulse_load', 'pulse_perf']);
    final load = memory.events.first.params;
    expect(load['kind'], 'cold_start');
    expect(load['duration_ms'], 2345.678);
    final p = memory.events.last.params;
    expect(p['frames'], 1800);
    expect(p['window_s'], 30.0);
    expect(p['ft_src'], 'flutter_ft');
    expect(p['ft_p50_ms'], 17.0);
    expect(p['ft_p95_ms'], 17.8);
    expect(p['ft_max_ms'], 40.0);
    expect(p['slow_frames'], 6);
    expect(p['janky_frames'], 1086);
    expect(p['cpu_main_p95_ms'], 7.12);
    expect(p['gpu_p95_ms'], 8.9);
    expect(p['tier'], 'low');
    expect(p['mem_src'], 'dart_rss');
    expect(p['sys_used_mb'], 600); // peak of the per-second samples
    expect(p['sys_mem_mb'], 3072);
    expect(p.length, lessThanOrEqualTo(25)); // GA4 limit
    for (final k in p.keys) {
      expect(RegExp(r'^[a-z][a-z0-9_]{0,39}$').hasMatch(k), isTrue, reason: k);
    }
  });

  test('JSON lines match the golden file the Python connector parses', () async {
    final dir = await Directory.systemTemp.createTemp('pulse');
    final (_, text) = await run(dir);
    final golden = File('test/golden/pulse_sample.jsonl');
    if (Platform.environment['UPDATE_GOLDEN'] == '1') {
      await golden.writeAsString(text);
    }
    expect(text, await golden.readAsString());
  });

  test('a partial window below 30 frames is not sent', () async {
    var now = 0;
    final memory = MemorySink();
    final perf = PulsePerf(
        sinks: [memory],
        processors: 8,
        clockMicros: () => now,
        wallClock: () => wall,
        rssBytes: () => 0);
    perf.onTimings(frames(1, 20, 0, processStart));
    now = 1000000;
    await perf.flush();
    expect(memory.events, isEmpty); // no processStart: no cold start either
  });

  test('timestamps are ISO 8601 UTC with milliseconds', () {
    expect(
        PulseEvent.isoMillis(DateTime.utc(2026, 10, 1, 9, 5, 7, 42)), '2026-10-01T09:05:07.042Z');
    expect(PulsePerf.newSession().length, 32);
  });
}
