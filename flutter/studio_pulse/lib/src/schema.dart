/// The studio-pulse client event contract (docs/EVENTS.md).
///
/// The same names as `unity/com.studio.pulse/Runtime/PulseSchema.cs` and
/// `src/studio_pulse/events.py`; tests/test_flutter_package.py in the Python
/// project fails if they drift apart.
class PulseSchema {
  PulseSchema._();

  static const int schemaVersion = 1;

  static const String perfEvent = 'pulse_perf';
  static const String loadEvent = 'pulse_load';

  static const String paramSchemaVersion = 'pulse_v';
  static const String paramTier = 'tier';
  static const String paramScene = 'scene';
  static const String paramWindowS = 'window_s';
  static const String paramFrames = 'frames';
  static const String paramFtSource = 'ft_src';
  static const String paramFtP50 = 'ft_p50_ms';
  static const String paramFtP95 = 'ft_p95_ms';
  static const String paramFtMax = 'ft_max_ms';
  static const String paramSlowFrames = 'slow_frames';
  static const String paramJankyFrames = 'janky_frames';
  static const String paramCpuMainP95 = 'cpu_main_p95_ms';
  static const String paramGpuP95 = 'gpu_p95_ms';
  static const String paramMemSource = 'mem_src';
  static const String paramSysUsed = 'sys_used_mb';
  static const String paramSysMem = 'sys_mem_mb';
  static const String paramKind = 'kind';
  static const String paramDuration = 'duration_ms';

  /// ft_src of Flutter windows: FrameTiming from SchedulerBinding.addTimingsCallback.
  static const String sourceFlutter = 'flutter_ft';

  /// mem_src of Flutter windows: dart:io ProcessInfo.currentRss (the OS view).
  static const String sourceRss = 'dart_rss';

  static const String loadKindColdStart = 'cold_start';
  static const String loadKindScene = 'scene_load';

  /// A frame is slow above two 60 Hz vsync intervals (below 30 fps).
  static const double slowFrameMs = 33.3;
}
