import 'dart:convert';
import 'dart:io';

/// Persisted per install in <data>/state.json (atomic replace). Written on lifecycle
/// changes and before queue files are deleted, never per event: the sequence number of
/// queued events is recovered from the queue itself. Same keys as the Unity SDK.
class PulseState {
  String? installId;
  int seq = 0;
  int snum = 0;
  String? sid;
  int sessionStartMs = 0;
  int lastActiveMs = 0;
  String? appVersion;
  bool foreground = false;
  bool cleanExit = false;
  bool fatalError = false;
  int launchMs = 0;
  String? consent;
  bool optedOut = false;
  int dropped = 0;
  final Map<int, int> attempts = <int, int>{};
  final List<String> pendingDeletes = <String>[];

  Map<String, Object?> toJson() => <String, Object?>{
        if (optedOut) 'opted_out': true,
        if (installId != null) 'install_id': installId,
        'seq': seq,
        'snum': snum,
        if (sid != null) 'sid': sid,
        'session_start_ms': sessionStartMs,
        'last_active_ms': lastActiveMs,
        if (appVersion != null) 'app_version': appVersion,
        'foreground': foreground,
        'clean_exit': cleanExit,
        'fatal_error': fatalError,
        'launch_ms': launchMs,
        if (consent != null) 'consent': consent,
        'dropped': dropped,
        'attempts': <String, int>{for (final e in attempts.entries) '${e.key}': e.value},
        'pending_deletes': pendingDeletes,
      };

  static PulseState? fromJson(Object? o) {
    if (o is! Map<String, Object?>) return null;
    int i(String k) => (o[k] is num) ? (o[k]! as num).toInt() : 0;
    bool b(String k) => o[k] == true;
    String? s(String k) => o[k] is String ? o[k]! as String : null;
    final st = PulseState()
      ..optedOut = b('opted_out')
      ..installId = s('install_id')
      ..seq = i('seq')
      ..snum = i('snum')
      ..sid = s('sid')
      ..sessionStartMs = i('session_start_ms')
      ..lastActiveMs = i('last_active_ms')
      ..appVersion = s('app_version')
      ..foreground = b('foreground')
      ..cleanExit = b('clean_exit')
      ..fatalError = b('fatal_error')
      ..launchMs = i('launch_ms')
      ..consent = s('consent')
      ..dropped = i('dropped');
    final a = o['attempts'];
    if (a is Map<String, Object?>) {
      a.forEach((k, v) {
        final lvl = int.tryParse(k);
        if (lvl != null && v is int) st.attempts[lvl] = v;
      });
    }
    final d = o['pending_deletes'];
    if (d is List<Object?>) st.pendingDeletes.addAll(d.whereType<String>());
    return st;
  }

  static PulseState? load(String path) {
    try {
      var f = File(path);
      if (!f.existsSync()) f = File('$path.tmp');
      if (!f.existsSync()) return null;
      return fromJson(jsonDecode(f.readAsStringSync()));
    } on Object {
      return null; // a corrupt state file starts a new install
    }
  }

  void save(String path) {
    final tmp = File('$path.tmp');
    tmp.parent.createSync(recursive: true);
    final raf = tmp.openSync(mode: FileMode.write);
    try {
      raf.writeFromSync(utf8.encode(jsonEncode(toJson())));
      raf.flushSync();
    } finally {
      raf.closeSync();
    }
    tmp.renameSync(path); // atomic replace (MoveFileEx with replace on Windows)
  }
}
