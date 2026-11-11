import 'dart:async';
import 'dart:io';
import 'dart:math';
import 'dart:ui' show FramePhase, FrameTiming;

import 'package:flutter/scheduler.dart';

import 'device_tier.dart';
import 'frame_window.dart';
import 'pulse_event.dart';
import 'schema.dart';
import 'sinks.dart';

/// Frame-time, start-time and memory telemetry on the studio-pulse contract.
///
/// Call [PulsePerf.markProcessStart] first thing in `main()`, then
/// [start] once the binding exists:
///
///     final perfStart = PulsePerf.markProcessStart();
///     void main() {
///       WidgetsFlutterBinding.ensureInitialized();
///       PulsePerf(sinks: [FirebaseAnalyticsSink(FirebaseAnalytics.instance.logEvent)],
///                 processStart: perfStart, deviceMemoryMb: ramMb).start();
///       runApp(const MyApp());
///     }
///
/// Per window (default 30 s) one `pulse_perf` event: frame total time
/// (vsync start to raster finish, FrameTiming.totalSpan) p50 / p95 / max,
/// frames over 33.3 ms (`slow_frames`) and over the display's frame budget
/// (`janky_frames`), UI-thread build p95 in `cpu_main_p95_ms` and raster-thread
/// p95 in `gpu_p95_ms` (Flutter does not expose GPU time; `ft_src` =
/// `flutter_ft` tells these windows apart), and the peak resident set size
/// (`sys_used_mb`, `mem_src` = `dart_rss`). Once per process a `pulse_load`
/// `cold_start`: from [markProcessStart] to the first rasterized frame (engine
/// start before `main()` is not included).
///
/// The timings callback is the only per-frame code: it reads integer
/// timestamps from the engine's FrameTiming objects and stores them in
/// preallocated rings; it creates no objects beyond the list the engine passes in.
class PulsePerf {
  PulsePerf({
    required this.sinks,
    DateTime? processStart,
    this.windowSeconds = 30,
    this.refreshRateHz = 60,
    int? deviceMemoryMb,
    int? processors,
    this.minFramesForPartialWindow = 30,
    int Function()? clockMicros,
    DateTime Function()? wallClock,
    int Function()? rssBytes,
  })  : _processStart = processStart,
        _deviceMemoryMb = deviceMemoryMb,
        _clock = clockMicros ?? _stopwatchClock,
        _wall = wallClock ?? DateTime.now,
        _rss = rssBytes ?? _currentRss,
        _budgetUs = (1000000 / refreshRateHz).round(),
        tier = DeviceTier.classify(
          memoryMb: deviceMemoryMb,
          processors: processors ?? Platform.numberOfProcessors,
        );

  /// The wall-clock time to measure the cold start from (call as early as possible).
  static DateTime markProcessStart() => DateTime.now();

  final List<PulseSink> sinks;
  final int windowSeconds;
  final double refreshRateHz;
  final int minFramesForPartialWindow;
  final String tier;

  /// The screen or route name to report with the next window (`scene`).
  String scene = '';

  final DateTime? _processStart;
  final int? _deviceMemoryMb;
  final int Function() _clock;
  final DateTime Function() _wall;
  final int Function() _rss;
  final FrameWindow _window = FrameWindow();
  int _windowStartUs = -1;
  int _lastMemUs = -1;
  int _peakRss = 0;
  bool _coldStartSent = false;
  bool _started = false;

  /// The display's frame budget: frames longer than this are janky.
  final int _budgetUs;

  void start([SchedulerBinding? binding]) {
    if (_started) return;
    _started = true;
    (binding ?? SchedulerBinding.instance).addTimingsCallback(onTimings);
  }

  void stop([SchedulerBinding? binding]) {
    if (!_started) return;
    _started = false;
    (binding ?? SchedulerBinding.instance).removeTimingsCallback(onTimings);
    unawaited(flush());
  }

  /// The per-frame path (also called directly by tests with synthetic timings).
  void onTimings(List<FrameTiming> timings) {
    final now = _clock();
    if (_windowStartUs < 0) _windowStartUs = now;
    for (var i = 0; i < timings.length; i++) {
      final t = timings[i];
      final vsync = t.timestampInMicroseconds(FramePhase.vsyncStart);
      final rasterEnd = t.timestampInMicroseconds(FramePhase.rasterFinish);
      _window.add(
        rasterEnd - vsync,
        t.timestampInMicroseconds(FramePhase.buildFinish) -
            t.timestampInMicroseconds(FramePhase.buildStart),
        rasterEnd - t.timestampInMicroseconds(FramePhase.rasterStart),
        _budgetUs,
      );
      if (!_coldStartSent) {
        _coldStartSent = true;
        _sendColdStart(t.timestampInMicroseconds(FramePhase.rasterFinishWallTime));
      }
    }
    if (now - _lastMemUs >= 1000000) {
      _lastMemUs = now;
      final rss = _rss();
      if (rss > _peakRss) _peakRss = rss;
    }
    if (now - _windowStartUs >= windowSeconds * 1000000) {
      _closeWindow(now);
    }
  }

  /// Send a partial window (app paused or closing) if it has enough frames,
  /// then flush the sinks. Call it from a WidgetsBindingObserver on `paused`.
  Future<void> flush() async {
    if (_window.frames >= minFramesForPartialWindow) _closeWindow(_clock());
    await Future.wait(sinks.map((s) => s.flush()));
  }

  void _sendColdStart(int firstFrameWallUs) {
    final start = _processStart;
    if (start == null) return;
    final ms = (firstFrameWallUs - start.microsecondsSinceEpoch) / 1000.0;
    if (ms <= 0 || ms > 600000) return;
    _emit(PulseEvent(PulseSchema.loadEvent, _wall(), <String, Object>{
      PulseSchema.paramSchemaVersion: PulseSchema.schemaVersion,
      PulseSchema.paramTier: tier,
      PulseSchema.paramKind: PulseSchema.loadKindColdStart,
      PulseSchema.paramScene: scene.isEmpty ? 'first_frame' : scene,
      PulseSchema.paramDuration: _round(ms),
    }));
  }

  void _closeWindow(int now) {
    final w = _window;
    if (w.frames > 0) {
      final params = <String, Object>{
        PulseSchema.paramSchemaVersion: PulseSchema.schemaVersion,
        PulseSchema.paramTier: tier,
        PulseSchema.paramScene: scene,
        PulseSchema.paramWindowS: _round((now - _windowStartUs) / 1e6),
        PulseSchema.paramFrames: w.frames,
        PulseSchema.paramFtSource: PulseSchema.sourceFlutter,
        PulseSchema.paramFtP50: _round(w.totalMs(0.5)),
        PulseSchema.paramFtP95: _round(w.totalMs(0.95)),
        PulseSchema.paramFtMax: _round(w.maxTotalUs / 1000.0),
        PulseSchema.paramSlowFrames: w.slowFrames,
        PulseSchema.paramJankyFrames: w.jankyFrames,
        PulseSchema.paramCpuMainP95: _round(w.buildMs(0.95)),
        PulseSchema.paramGpuP95: _round(w.rasterMs(0.95)),
      };
      if (_peakRss > 0) {
        params[PulseSchema.paramMemSource] = PulseSchema.sourceRss;
        params[PulseSchema.paramSysUsed] = (_peakRss / (1024 * 1024)).round();
      }
      final mem = _deviceMemoryMb;
      if (mem != null && mem > 0) params[PulseSchema.paramSysMem] = mem;
      _emit(PulseEvent(PulseSchema.perfEvent, _wall(), params));
    }
    w.reset();
    _windowStartUs = now;
    _peakRss = 0;
    _lastMemUs = -1;
  }

  void _emit(PulseEvent e) {
    for (final s in sinks) {
      s.send(e);
    }
  }

  static double _round(double v) => (v * 1000).roundToDouble() / 1000;

  static final Stopwatch _sw = Stopwatch()..start();
  static int _stopwatchClock() => _sw.elapsedMicroseconds;
  static int _currentRss() => ProcessInfo.currentRss;

  /// A random id per app launch (no user or device identifier).
  static String newSession([Random? random]) {
    final r = random ?? Random.secure();
    const hex = '0123456789abcdef';
    return List<String>.generate(32, (_) => hex[r.nextInt(16)]).join();
  }
}
