/// Device tiers, the rules of the Unity package's DeviceTierRules.Classify.
///
///   low   memory < 3500 MB, or processors <= 4, or reported graphics memory < 512 MB
///   high  memory >= 7000 MB and processors >= 6 (and graphics >= 2048 MB when reported)
///   mid   everything else
///
/// Pure Dart cannot read the device's memory: pass it in (for example from the
/// device_info_plus package). Without it only `low` (by processors) or
/// `unknown` can be decided.
class DeviceTier {
  DeviceTier._();

  static String classify({int? memoryMb, required int processors, int? graphicsMemoryMb}) {
    if (processors <= 4) return 'low';
    if (graphicsMemoryMb != null && graphicsMemoryMb > 0 && graphicsMemoryMb < 512) return 'low';
    if (memoryMb == null || memoryMb <= 0) return 'unknown';
    if (memoryMb < 3500) return 'low';
    final gpuOk = graphicsMemoryMb == null || graphicsMemoryMb <= 0 || graphicsMemoryMb >= 2048;
    if (memoryMb >= 7000 && processors >= 6 && gpuOk) return 'high';
    return 'mid';
  }
}
