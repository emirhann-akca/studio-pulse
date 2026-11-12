import 'dart:convert';

import '../contract.g.dart';
import 'json_writer.dart';

/// Error scrubbing and fingerprints: the same rules as the Unity SDK's PulseErrors.
class PulseErrors {
  PulseErrors._();

  static const int msgMaxLen = 256;
  static const int frameMaxLen = 100;
  static const int typeMaxLen = 100;

  /// First line with anything that can identify a person or a device replaced: tokens
  /// with '@' -> <email>, with '/' or '\' -> <path>, 0x... -> <hex>, long hex/uuid
  /// tokens -> <id>, digit runs -> #. At most 256 characters.
  static String scrub(String? message) {
    if (message == null || message.isEmpty) return '';
    final nl = message.indexOf('\n');
    final line = (nl >= 0 ? message.substring(0, nl) : message).trim();
    final tokens = line.split(' ');
    final sb = StringBuffer();
    for (var t = 0; t < tokens.length; t++) {
      if (t > 0) sb.write(' ');
      final tok = tokens[t];
      if (tok.contains('@')) {
        sb.write('<email>');
      } else if (tok.contains('/') || tok.contains(r'\')) {
        sb.write('<path>');
      } else if (tok.length > 2 && tok[0] == '0' && (tok[1] == 'x' || tok[1] == 'X')) {
        sb.write('<hex>');
      } else if (_idLike(tok)) {
        sb.write('<id>');
      } else {
        var inDigits = false;
        for (var i = 0; i < tok.length; i++) {
          final c = tok.codeUnitAt(i);
          if (c >= 0x30 && c <= 0x39) {
            if (!inDigits) sb.write('#');
            inDigits = true;
          } else {
            inDigits = false;
            sb.writeCharCode(c);
          }
        }
      }
    }
    final s = sb.toString();
    return s.substring(0, PulseJsonWriter.cutLength(s, msgMaxLen));
  }

  static bool _idLike(String tok) {
    if (tok.length < 8) return false;
    var digit = false;
    for (var i = 0; i < tok.length; i++) {
      final c = tok.codeUnitAt(i);
      final isDigit = c >= 0x30 && c <= 0x39;
      final hex = isDigit || (c >= 0x61 && c <= 0x66) || (c >= 0x41 && c <= 0x46);
      if (!hex && c != 0x2D) return false;
      digit = digit || isDigit;
    }
    return digit;
  }

  /// The first stack frame as "Type.method" (Dart, Unity, .NET formats).
  static String? topFrame(String? stack) {
    if (stack == null || stack.isEmpty) return null;
    for (final raw in stack.split('\n')) {
      var l = raw.trim();
      if (l.isEmpty) continue;
      if (l.startsWith('#')) {
        var j = 1;
        while (j < l.length && l.codeUnitAt(j) >= 0x30 && l.codeUnitAt(j) <= 0x39) {
          j++;
        }
        l = l.substring(j).trim();
      }
      if (l.startsWith('at ')) l = l.substring(3).trim();
      final p = l.indexOf('(');
      if (p >= 0) l = l.substring(0, p).trim();
      if (l.isEmpty) continue;
      return l.substring(0, PulseJsonWriter.cutLength(l, frameMaxLen));
    }
    return null;
  }

  /// FNV-1a 64 of UTF-8 "type\nframe\nscrubbed", 16 lowercase hex digits.
  static String fingerprint(String type, String? frame, String scrubbed) {
    var h = 0xcbf29ce484222325;
    for (final b in utf8.encode('$type\n${frame ?? ''}\n$scrubbed')) {
      h ^= b;
      h *= 0x100000001b3; // wraps modulo 2^64 on native platforms
    }
    String part(int v) => v.toRadixString(16).padLeft(8, '0');
    return part((h >>> 32) & 0xFFFFFFFF) + part(h & 0xFFFFFFFF);
  }
}

/// Installer / iOS environment -> install_store (same table as the Unity SDK).
class PulseInstallStores {
  PulseInstallStores._();

  static String fromAndroidInstaller(String? installer) {
    if (installer == null || installer.isEmpty) return PulseInstallStore.sideload;
    switch (installer) {
      case 'com.android.vending':
      case 'com.google.android.feedback':
        return PulseInstallStore.googlePlay;
      case 'com.amazon.venezia':
        return PulseInstallStore.amazon;
      case 'com.sec.android.app.samsungapps':
        return PulseInstallStore.samsung;
      case 'com.huawei.appmarket':
        return PulseInstallStore.huawei;
      case 'com.xiaomi.market':
      case 'com.xiaomi.mipicks':
      case 'com.xiaomi.discover':
        return PulseInstallStore.xiaomi;
      case 'com.google.android.packageinstaller':
      case 'com.android.packageinstaller':
      case 'com.android.shell':
        return PulseInstallStore.sideload;
      default:
        return PulseInstallStore.unknown;
    }
  }

  static String fromIos(String? native) {
    switch (native) {
      case 'production':
      case 'app_store':
        return PulseInstallStore.appStore;
      case 'sandbox':
      case 'testflight':
        return PulseInstallStore.testflight;
      case 'xcode':
      case 'sideload':
        return PulseInstallStore.sideload;
      case 'simulator':
        return PulseInstallStore.simulator;
      default:
        return PulseInstallStore.unknown;
    }
  }
}
