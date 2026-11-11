import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

/// Static guard (as for the Unity package): the per-frame code must not create
/// objects. FrameTiming lists come from the engine; everything else is ints
/// stored into preallocated typed lists.
/// The body of a method: from the brace after its signature to the next marker.
String body(String src, String start, String end) {
  final open = src.indexOf('{', src.indexOf(start));
  return src.substring(open, src.indexOf(end));
}

void main() {
  test('the timings callback and FrameWindow.add allocate nothing', () {
    final perf = File('lib/src/pulse_perf.dart').readAsStringSync();
    final perFrame = body(perf, 'void onTimings(', 'Future<void> flush(');
    final window = File('lib/src/frame_window.dart').readAsStringSync();
    final add = body(window, 'void add(', 'double totalMs(');
    for (final code in [perFrame, add]) {
      for (final bad in [
        'new ',
        '<String',
        '[]',
        '{}',
        'Duration',
        'toString',
        r'$',
        '.map(',
        'List<',
        'DateTime'
      ]) {
        expect(code.contains(bad), isFalse, reason: 'per-frame code contains "$bad"');
      }
    }
  });
}
