import 'dart:typed_data';

/// Per-frame durations of one window in preallocated rings.
///
/// [add] runs once per frame and allocates nothing: three stores into typed
/// lists and a few counters. Percentiles are computed only when the window
/// closes, by copying the ring into a preallocated scratch list and sorting
/// it in place; they use linear interpolation between closest ranks, the same
/// definition as the Unity package and DuckDB's quantile_cont.
class FrameWindow {
  FrameWindow({this.capacity = 4096})
      : _total = Int64List(capacity),
        _build = Int64List(capacity),
        _raster = Int64List(capacity),
        _scratch = Float64List(capacity);

  /// Frames kept for percentiles (4096 covers a 30 s window up to ~136 fps).
  final int capacity;
  final Int64List _total;
  final Int64List _build;
  final Int64List _raster;
  final Float64List _scratch;
  int _stored = 0;
  int _next = 0;

  /// Every frame of the window, also those beyond the ring's capacity.
  int frames = 0;
  int slowFrames = 0;
  int jankyFrames = 0;
  int maxTotalUs = 0;

  static const int slowUs = 33300;

  void add(int totalUs, int buildUs, int rasterUs, int budgetUs) {
    _total[_next] = totalUs;
    _build[_next] = buildUs;
    _raster[_next] = rasterUs;
    _next = _next + 1 == capacity ? 0 : _next + 1;
    if (_stored < capacity) _stored++;
    frames++;
    if (totalUs > slowUs) slowFrames++;
    if (totalUs > budgetUs) jankyFrames++;
    if (totalUs > maxTotalUs) maxTotalUs = totalUs;
  }

  double totalMs(double q) => _percentileMs(_total, q);
  double buildMs(double q) => _percentileMs(_build, q);
  double rasterMs(double q) => _percentileMs(_raster, q);

  double _percentileMs(Int64List src, double q) {
    final n = _stored;
    if (n == 0) return 0;
    for (var i = 0; i < n; i++) {
      _scratch[i] = src[i] / 1000.0;
    }
    final view = Float64List.sublistView(_scratch, 0, n)..sort();
    final pos = q * (n - 1);
    final lo = pos.floor();
    final hi = lo + 1 < n ? lo + 1 : n - 1;
    return view[lo] + (view[hi] - view[lo]) * (pos - lo);
  }

  void reset() {
    _stored = 0;
    _next = 0;
    frames = 0;
    slowFrames = 0;
    jankyFrames = 0;
    maxTotalUs = 0;
  }
}
