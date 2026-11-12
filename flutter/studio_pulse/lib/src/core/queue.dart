import 'dart:io';
import 'dart:typed_data';

import '../contract.g.dart';
import 'json_writer.dart';

/// One queue file: a context line, then one event per line.
class _Segment {
  _Segment(this.no, this.path, this.ctx);

  final int no;
  final String path;
  final Uint8List ctx;
  int count = 0;
  int acked = 0;
  int bytes = 0;
  int maxSeq = 0;
  bool closed = false;
  int inFlight = 0;

  int get pending => count - acked;
  String get ackPath => '${path.substring(0, path.length - 4)}.ack';
}

class QueuePart {
  QueuePart(this._seg, this.count);

  final _Segment _seg;
  final int count;
}

/// The on-device event queue, the same design as the Unity SDK's PulseQueue:
/// append-only segment files written with one write per event (a killed process loses
/// nothing already recorded), fsync when a segment closes and on background, deleted
/// only after a 2xx, a ".ack" file for partly delivered segments, and a cap that drops
/// the oldest segments first and counts them.
class PulseQueue {
  PulseQueue(this._dir, this._log);

  final String _dir;
  final void Function(String)? _log;
  final List<_Segment> _segs = <_Segment>[];
  _Segment? _active;
  RandomAccessFile? _file;
  int _nextNo = 1;

  int maxEvents = PulseLimits.queueMaxEvents;
  int maxBytes = PulseLimits.queueMaxBytes;
  int segmentMaxEvents = 100;
  int segmentMaxBytes = 256 * 1024;

  int pendingEvents = 0;
  int totalBytes = 0;

  /// Events removed by the cap since this object was created.
  int dropped = 0;

  static const int _seqAt = 7 + 36 + 8;
  static final Uint8List _idPrefix = Uint8List.fromList('{"id":"'.codeUnits);
  static final Uint8List _seqKey = Uint8List.fromList('","seq":'.codeUnits);

  /// Reads the queue directory; returns the highest seq found.
  int load() {
    Directory(_dir).createSync(recursive: true);
    var maxSeq = 0;
    final files = Directory(_dir).listSync().whereType<File>().map((f) => f.path).where((p) => p.endsWith('.seg')).toList()
      ..sort();
    for (final f in files) {
      try {
        final s = _read(f);
        if (s == null) continue;
        if (s.maxSeq > maxSeq) maxSeq = s.maxSeq;
        if (s.pending <= 0) {
          _deleteFiles(s);
          continue;
        }
        _segs.add(s);
        pendingEvents += s.pending;
        totalBytes += s.bytes;
        if (s.no >= _nextNo) _nextNo = s.no + 1;
      } on Object catch (e) {
        _warn('queue: unreadable segment $f: $e');
      }
    }
    for (final f in Directory(_dir).listSync().whereType<File>()) {
      if (f.path.endsWith('.ack') && !File('${f.path.substring(0, f.path.length - 4)}.seg').existsSync()) {
        _tryDelete(f.path);
      }
    }
    return maxSeq;
  }

  _Segment? _read(String path) {
    final data = File(path).readAsBytesSync();
    final name = path.split(Platform.pathSeparator).last.split('/').last;
    final no = int.tryParse(name.substring(0, name.length - 4));
    if (no == null) return null;
    var end = data.length;
    while (end > 0 && data[end - 1] != 0x0A) {
      end--; // a torn last line is dropped
    }
    final nl = data.indexOf(0x0A);
    if (nl <= 0 || nl >= end) {
      _tryDelete(path);
      return null;
    }
    final seg = _Segment(no, path, Uint8List.fromList(data.sublist(0, nl)))..closed = true;
    final good = <List<int>>[];
    var rewrite = end != data.length;
    var pos = nl + 1;
    while (pos < end) {
      final e = data.indexOf(0x0A, pos);
      if (_validLine(data, pos, e - pos)) {
        good.add(<int>[pos, e - pos]);
        final seq = _parseSeq(data, pos);
        if (seq > seg.maxSeq) seg.maxSeq = seq;
      } else {
        rewrite = true;
      }
      pos = e + 1;
    }
    seg.count = good.length;
    if (rewrite) {
      final raf = File(path).openSync(mode: FileMode.write);
      try {
        raf.writeFromSync(data, 0, nl + 1);
        for (final g in good) {
          raf.writeFromSync(data, g[0], g[0] + g[1]);
          raf.writeByteSync(0x0A);
        }
        raf.flushSync();
      } finally {
        raf.closeSync();
      }
      _tryDelete(seg.ackPath); // line numbers changed; a re-send is deduped by the collector
      seg.bytes = File(path).lengthSync();
    } else {
      seg.bytes = data.length;
      final ack = File(seg.ackPath);
      if (ack.existsSync()) {
        final n = int.tryParse(ack.readAsStringSync().trim());
        if (n != null) seg.acked = n < seg.count ? n : seg.count;
      }
    }
    return seg;
  }

  static bool _validLine(Uint8List d, int o, int len) {
    if (len < _seqAt + 2 || d[o + len - 1] != 0x7D) return false;
    for (var i = 0; i < _idPrefix.length; i++) {
      if (d[o + i] != _idPrefix[i]) return false;
    }
    for (var i = 0; i < _seqKey.length; i++) {
      if (d[o + 43 + i] != _seqKey[i]) return false;
    }
    return d[o + _seqAt] >= 0x30 && d[o + _seqAt] <= 0x39;
  }

  static int _parseSeq(Uint8List d, int o) {
    var v = 0;
    for (var i = o + _seqAt; i < d.length && d[i] >= 0x30 && d[i] <= 0x39; i++) {
      v = v * 10 + (d[i] - 0x30);
    }
    return v;
  }

  /// Appends one event line (the writer holds the JSON and its trailing newline).
  void append(Uint8List ctx, PulseJsonWriter line, int seq) {
    final a = _active;
    if (a == null || !identical(a.ctx, ctx) || a.count >= segmentMaxEvents || a.bytes >= segmentMaxBytes) {
      closeActive(fsync: true);
      _open(ctx);
    }
    final seg = _active!;
    _file!.writeFromSync(line.buffer, 0, line.length);
    seg.count++;
    seg.bytes += line.length;
    if (seq > seg.maxSeq) seg.maxSeq = seq;
    pendingEvents++;
    totalBytes += line.length;
    _enforceCap();
  }

  void _open(Uint8List ctx) {
    Directory(_dir).createSync(recursive: true);
    final no = _nextNo++;
    final path = '$_dir${Platform.pathSeparator}${no.toString().padLeft(12, '0')}.seg';
    final raf = File(path).openSync(mode: FileMode.writeOnly);
    raf.writeFromSync(ctx);
    raf.writeByteSync(0x0A);
    _file = raf;
    final seg = _Segment(no, path, ctx)..bytes = ctx.length + 1;
    _active = seg;
    _segs.add(seg);
    totalBytes += seg.bytes;
  }

  /// fsync the open segment (app going to the background).
  void sync() {
    try {
      _file?.flushSync();
    } on Object catch (e) {
      _warn('queue: fsync failed: $e');
    }
  }

  void closeActive({bool fsync = true}) {
    final f = _file;
    if (f != null) {
      try {
        if (fsync) f.flushSync();
        f.closeSync();
      } on Object catch (e) {
        _warn('queue: close failed: $e');
      }
      _file = null;
    }
    final a = _active;
    if (a != null) {
      a.closed = true;
      if (a.pending == 0 && a.inFlight == 0) _remove(a);
      _active = null;
    }
  }

  bool get hasActivePending => _active != null && _active!.pending > 0;

  /// Writes a v2 batch body into [body] from the oldest pending events (closed segments
  /// with the same context, at most [maxEvents] / [maxBytes]) and marks them in flight.
  List<QueuePart>? buildBatch(PulseJsonWriter body, int sentAt, int maxEvents, int maxBytes) {
    if (hasActivePending) closeActive(fsync: true);
    List<QueuePart>? parts;
    Uint8List? ctx;
    var events = 0;
    for (final s in _segs) {
      if (events >= maxEvents) break;
      if (!s.closed || s.pending <= 0 || s.inFlight > 0) continue;
      if (ctx != null && !_same(ctx, s.ctx)) break;
      Uint8List data;
      try {
        data = File(s.path).readAsBytesSync();
      } on Object catch (e) {
        _warn('queue: cannot read ${s.path}: $e');
        continue;
      }
      var pos = data.indexOf(0x0A) + 1;
      for (var skip = 0; skip < s.acked && pos > 0; skip++) {
        pos = data.indexOf(0x0A, pos) + 1;
      }
      if (pos <= 0) continue;
      var taken = 0;
      while (events < maxEvents && pos < data.length) {
        final e = data.indexOf(0x0A, pos);
        if (e < 0) break;
        final len = e - pos;
        if (ctx == null) {
          ctx = s.ctx;
          body
            ..clear()
            ..ascii('{"v":2,"schema":"')
            ..ascii(PulseContract.version)
            ..ascii('","sent_at":')
            ..integer(sentAt)
            ..ascii(',"ctx":')
            ..bytes(ctx)
            ..ascii(',"events":[');
        }
        if (events > 0 && body.length + len + 3 > maxBytes) {
          events = maxEvents;
          break;
        }
        if (events > 0) body.byte(0x2C);
        body.bytes(data, pos, e);
        events++;
        taken++;
        pos = e + 1;
      }
      if (taken > 0) {
        (parts ??= <QueuePart>[]).add(QueuePart(s, taken));
        s.inFlight = taken;
      }
      if (taken < s.pending) break; // keep events in order
    }
    if (parts == null) return null;
    body.ascii(']}');
    return parts;
  }

  static bool _same(Uint8List a, Uint8List b) {
    if (identical(a, b)) return true;
    if (a.length != b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (a[i] != b[i]) return false;
    }
    return true;
  }

  /// The request ended: delivered (or refused for good) events leave the queue.
  void complete(List<QueuePart> parts, {required bool delivered}) {
    for (final p in parts) {
      final s = p._seg;
      s.inFlight = 0;
      if (!delivered || !_segs.contains(s)) continue;
      s.acked += p.count;
      pendingEvents -= p.count;
      if (s.pending <= 0 && s.closed) {
        _remove(s);
      } else {
        _writeAck(s);
      }
    }
  }

  void _writeAck(_Segment s) {
    try {
      final tmp = File('${s.ackPath}.tmp')..writeAsStringSync('${s.acked}');
      tmp.renameSync(s.ackPath);
    } on Object catch (e) {
      _warn('queue: ack write failed (a re-send is deduped): $e');
    }
  }

  void _remove(_Segment s) {
    _segs.remove(s);
    totalBytes -= s.bytes;
    if (s.pending > 0) pendingEvents -= s.pending;
    _deleteFiles(s);
  }

  void _deleteFiles(_Segment s) {
    _tryDelete(s.path);
    _tryDelete(s.ackPath);
  }

  void _enforceCap() {
    while ((pendingEvents > maxEvents || totalBytes > maxBytes) && _segs.isNotEmpty) {
      _Segment? victim;
      for (final s in _segs) {
        if (s.inFlight == 0) {
          victim = s;
          break;
        }
      }
      if (victim == null) return;
      if (identical(victim, _active)) {
        if (_segs.length == 1 && victim.pending <= 1) return; // keep the event just written
        closeActive(fsync: false);
        if (!_segs.contains(victim)) continue;
      }
      dropped += victim.pending;
      _remove(victim);
    }
  }

  /// Deletes every queued event (opt-out, consent refused, deletion request).
  void wipe() {
    closeActive(fsync: false);
    for (final s in List<_Segment>.of(_segs)) {
      _remove(s);
    }
    _segs.clear();
    pendingEvents = 0;
    totalBytes = 0;
    try {
      final d = Directory(_dir);
      if (d.existsSync()) {
        for (final f in d.listSync().whereType<File>()) {
          _tryDelete(f.path);
        }
      }
    } on Object catch (e) {
      _warn('queue: wipe failed: $e');
    }
  }

  void _tryDelete(String path) {
    try {
      final f = File(path);
      if (f.existsSync()) f.deleteSync();
    } on Object catch (e) {
      _warn('queue: delete failed: $e');
    }
  }

  void _warn(String m) => _log?.call(m);
}
