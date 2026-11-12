import 'package:flutter_test/flutter_test.dart';
import 'package:studio_pulse/studio_pulse_perf.dart';

void main() {
  test('percentiles interpolate between closest ranks (quantile_cont)', () {
    final w = FrameWindow(capacity: 16);
    for (final ms in [10, 20, 30, 40]) {
      w.add(ms * 1000, ms * 400, ms * 500, 16667);
    }
    // n = 4: p50 at position 1.5 -> 25 ms; p95 at 2.85 -> 30 + 0.85 x 10 = 38.5 ms
    expect(w.totalMs(0.5), closeTo(25.0, 1e-9));
    expect(w.totalMs(0.95), closeTo(38.5, 1e-9));
    expect(w.buildMs(0.5), closeTo(10.0, 1e-9));
    expect(w.frames, 4);
    expect(w.slowFrames, 1); // 40 ms > 33.3 ms
    expect(w.jankyFrames, 3); // 20, 30, 40 ms > 16.667 ms
    expect(w.maxTotalUs, 40000);
  });

  test('the ring keeps the most recent frames and counts every frame', () {
    final w = FrameWindow(capacity: 3);
    for (final ms in [50, 10, 11, 12]) {
      w.add(ms * 1000, 0, 0, 16667);
    }
    expect(w.frames, 4);
    expect(w.totalMs(1.0), closeTo(12.0, 1e-9)); // 50 ms was overwritten
    expect(w.maxTotalUs, 50000); // the max sees every frame
    w.reset();
    expect(w.frames, 0);
    expect(w.totalMs(0.5), 0);
  });

  test('device tiers follow the Unity rules', () {
    expect(DeviceTier.classify(memoryMb: 3072, processors: 8), 'low');
    expect(DeviceTier.classify(memoryMb: 8192, processors: 4), 'low');
    expect(DeviceTier.classify(memoryMb: 6144, processors: 8), 'mid');
    expect(DeviceTier.classify(memoryMb: 8192, processors: 8), 'high');
    expect(DeviceTier.classify(memoryMb: 8192, processors: 8, graphicsMemoryMb: 1024), 'mid');
    expect(DeviceTier.classify(processors: 8), 'unknown');
  });
}
