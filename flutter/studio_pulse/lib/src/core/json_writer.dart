import 'dart:typed_data';

import 'environment.dart';

/// Compact JSON bytes, byte for byte the format of the Unity SDK's PulseJsonWriter
/// (pinned by contract/golden/sdk_conformance.ndjson):
///   - strings: UTF-8; escapes \" \\ \b \f \n \r \t, other controls as \u00xx; a cut
///     never splits a surrogate pair; a lone surrogate becomes U+FFFD
///   - doubles: the shortest digits that read back to the same double, laid out like
///     ECMAScript Number#toString (0.1, 50, 1e-7, 1e+21); -0 is 0
class PulseJsonWriter {
  PulseJsonWriter([int capacity = 1024]) : buffer = Uint8List(capacity);

  Uint8List buffer;
  int length = 0;

  static const String _hex = '0123456789abcdef';

  void clear() => length = 0;

  void _ensure(int extra) {
    final need = length + extra;
    if (need <= buffer.length) return;
    var size = buffer.length * 2;
    while (size < need) {
      size *= 2;
    }
    final grown = Uint8List(size)..setRange(0, length, buffer);
    buffer = grown;
  }

  Uint8List toBytes() => Uint8List.fromList(buffer.sublist(0, length));

  void byte(int b) {
    _ensure(1);
    buffer[length++] = b;
  }

  void bytes(List<int> src, [int start = 0, int? end]) {
    final e = end ?? src.length;
    _ensure(e - start);
    buffer.setRange(length, length + e - start, src, start);
    length += e - start;
  }

  /// ASCII text written as is (keys, names, punctuation).
  void ascii(String s) {
    _ensure(s.length);
    for (var i = 0; i < s.length; i++) {
      buffer[length++] = s.codeUnitAt(i);
    }
  }

  /// `,"key":` (the comma only when [comma]).
  void key(String k, bool comma) {
    if (comma) byte(0x2C);
    byte(0x22);
    ascii(k);
    byte(0x22);
    byte(0x3A);
  }

  void nullValue() => ascii('null');

  void boolValue(bool v) => ascii(v ? 'true' : 'false');

  /// UTF-16 units of [s] kept under [maxLen] (never half a surrogate pair).
  static int cutLength(String s, int maxLen) {
    if (s.length <= maxLen) return s.length;
    var n = maxLen;
    if (n > 0 && _isHigh(s.codeUnitAt(n - 1))) n--;
    return n;
  }

  static bool _isHigh(int c) => c >= 0xD800 && c <= 0xDBFF;
  static bool _isLow(int c) => c >= 0xDC00 && c <= 0xDFFF;

  void string(String s, [int maxLen = 1 << 30]) {
    final n = cutLength(s, maxLen);
    _ensure(n * 6 + 2);
    final b = buffer;
    var p = length;
    b[p++] = 0x22;
    for (var i = 0; i < n; i++) {
      final c = s.codeUnitAt(i);
      if (c < 0x80) {
        switch (c) {
          case 0x22:
            b[p++] = 0x5C;
            b[p++] = 0x22;
          case 0x5C:
            b[p++] = 0x5C;
            b[p++] = 0x5C;
          case 0x08:
            b[p++] = 0x5C;
            b[p++] = 0x62;
          case 0x0C:
            b[p++] = 0x5C;
            b[p++] = 0x66;
          case 0x0A:
            b[p++] = 0x5C;
            b[p++] = 0x6E;
          case 0x0D:
            b[p++] = 0x5C;
            b[p++] = 0x72;
          case 0x09:
            b[p++] = 0x5C;
            b[p++] = 0x74;
          default:
            if (c < 0x20) {
              b[p++] = 0x5C;
              b[p++] = 0x75;
              b[p++] = 0x30;
              b[p++] = 0x30;
              b[p++] = _hex.codeUnitAt(c >> 4);
              b[p++] = _hex.codeUnitAt(c & 0xF);
            } else {
              b[p++] = c;
            }
        }
      } else if (c < 0x800) {
        b[p++] = 0xC0 | (c >> 6);
        b[p++] = 0x80 | (c & 0x3F);
      } else if (_isHigh(c) && i + 1 < n && _isLow(s.codeUnitAt(i + 1))) {
        final cp = 0x10000 + ((c - 0xD800) << 10) + (s.codeUnitAt(i + 1) - 0xDC00);
        i++;
        b[p++] = 0xF0 | (cp >> 18);
        b[p++] = 0x80 | ((cp >> 12) & 0x3F);
        b[p++] = 0x80 | ((cp >> 6) & 0x3F);
        b[p++] = 0x80 | (cp & 0x3F);
      } else if (c >= 0xD800 && c <= 0xDFFF) {
        b[p++] = 0xEF; // U+FFFD for a lone surrogate
        b[p++] = 0xBF;
        b[p++] = 0xBD;
      } else {
        b[p++] = 0xE0 | (c >> 12);
        b[p++] = 0x80 | ((c >> 6) & 0x3F);
        b[p++] = 0x80 | (c & 0x3F);
      }
    }
    b[p++] = 0x22;
    length = p;
  }

  void integer(int v) => ascii(v.toString());

  /// A finite double in the shared shortest form (callers skip NaN and infinities).
  void doubleValue(double v) => ascii(formatDouble(v));

  /// Dart's double.toString is already the shortest round-trip form in ECMAScript
  /// layout, except that integral values end in ".0".
  static String formatDouble(double v) {
    if (v.isNaN || v.isInfinite) return 'null';
    if (v == 0) return '0';
    if (v.abs() < 9007199254740992.0 && v == v.truncateToDouble()) return v.toInt().toString();
    final s = v.toString();
    return s.endsWith('.0') ? s.substring(0, s.length - 2) : s;
  }

  void uuid(Uint8List b) {
    byte(0x22);
    ascii(uuidString(b));
    byte(0x22);
  }

  static String uuidString(Uint8List b) {
    final sb = StringBuffer();
    for (var i = 0; i < 16; i++) {
      if (i == 4 || i == 6 || i == 8 || i == 10) sb.write('-');
      sb
        ..write(_hex[b[i] >> 4])
        ..write(_hex[b[i] & 0xF]);
    }
    return sb.toString();
  }
}

/// UUIDv7 (RFC 9562) for event and session ids, UUIDv4 for install ids.
class PulseUuid {
  PulseUuid._();

  /// 48-bit Unix ms, version 7, 74 random bits; draws exactly 10 random bytes.
  static Uint8List v7(int unixMs, PulseRandom rnd) {
    final b = Uint8List(16);
    b[0] = (unixMs >> 40) & 0xFF;
    b[1] = (unixMs >> 32) & 0xFF;
    b[2] = (unixMs >> 24) & 0xFF;
    b[3] = (unixMs >> 16) & 0xFF;
    b[4] = (unixMs >> 8) & 0xFF;
    b[5] = unixMs & 0xFF;
    rnd.nextBytes(b, 6, 10);
    b[6] = 0x70 | (b[6] & 0x0F);
    b[8] = 0x80 | (b[8] & 0x3F);
    return b;
  }

  /// Random UUIDv4; draws exactly 16 random bytes.
  static Uint8List v4(PulseRandom rnd) {
    final b = Uint8List(16);
    rnd.nextBytes(b, 0, 16);
    b[6] = 0x40 | (b[6] & 0x0F);
    b[8] = 0x80 | (b[8] & 0x3F);
    return b;
  }
}
