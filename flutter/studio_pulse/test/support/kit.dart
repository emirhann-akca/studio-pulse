// Test helpers for the Dart core: a fake clock, a synchronous fake transport, a
// collector-like dedupe, and a real in-process HttpServer for the HTTP status cases.
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:studio_pulse/core.dart';

class FakeClock implements PulseClock {
  FakeClock(this.now);

  int now;
  int mono = 1000000;

  @override
  int nowMs() => now;

  @override
  int monotonicMs() => mono;

  void advance(int ms) {
    now += ms;
    mono += ms;
  }
}

class Sent {
  Sent(this.url, this.gzip, this.raw)
      : json = utf8.decode(gzip ? GZipCodec().decode(raw) : raw);

  final String url;
  final bool gzip;
  final Uint8List raw;
  final String json;

  Map<String, Object?> get body => jsonDecode(json) as Map<String, Object?>;
  List<Map<String, Object?>> get events => (body['events']! as List<Object?>).cast<Map<String, Object?>>();
}

/// The collector's dedupe in miniature: one row per event id and per (install_id, seq).
class FakeCollector {
  final List<Map<String, Object?>> rows = <Map<String, Object?>>[];
  final Set<String> _ids = <String>{};
  final Set<String> _keys = <String>{};
  int duplicates = 0;

  void receive(Map<String, Object?> body) {
    final ctx = body['ctx']! as Map<String, Object?>;
    final install = ctx['install_id']! as String;
    for (final ev in (body['events']! as List<Object?>).cast<Map<String, Object?>>()) {
      final id = ev['id']! as String;
      final key = '$install/${ev['seq']}';
      if (_ids.contains(id) || _keys.contains(key)) {
        duplicates++;
        continue;
      }
      _ids.add(id);
      _keys.add(key);
      rows.add(<String, Object?>{...ev, 'install_id': install, 'app_version': ctx['app_version']});
    }
  }

  List<int> seqs([String? install]) =>
      rows.where((r) => install == null || r['install_id'] == install).map((r) => r['seq']! as int).toList()..sort();

  List<String> names() => rows.map((r) => r['name']! as String).toList();

  List<Map<String, Object?>> named(String n) => rows.where((r) => r['name'] == n).toList();
}

typedef Responder = PulseHttpResult? Function(Sent s);

class FakeTransport implements PulseTransport {
  final List<Sent> requests = <Sent>[];
  final FakeCollector collector = FakeCollector();
  Responder responder = ok;

  static PulseHttpResult? ok(Sent s) =>
      PulseHttpResult(s.url.endsWith('/v2/delete') ? 202 : 200, body: '{"ok":true}');

  @override
  void post(String url, String writeKey, Uint8List body, bool gzip, void Function(PulseHttpResult) done) {
    final s = Sent(url, gzip, Uint8List.fromList(body));
    requests.add(s);
    final r = responder(s);
    if (r != null) {
      if (r.status >= 200 && r.status < 300 && url.endsWith('/v2/batch')) collector.receive(s.body);
      done(r);
    }
  }

  List<Sent> get batches => requests.where((r) => r.url.endsWith('/v2/batch')).toList();
}

PulseDevice testDevice() => PulseDevice(
      appId: 'com.example.puzzle',
      platform: 'android',
      appVersion: '1.4.0',
      build: '140',
      buildEnv: 'release',
      installStore: 'google_play',
      installerRaw: 'com.android.vending',
      appName: 'Example Puzzle',
      sdk: PulseCore.sdkName,
      engine: 'flutter',
      os: 'android 14',
      deviceModel: 'SM-A145F',
      deviceTier: 'low',
      ramMb: 3072,
      locale: 'tr-TR',
      tzOffsetMin: 180,
    );

/// A device, a data directory and a core that can be restarted on the same directory.
class Rig {
  Rig({int seed = 7, int startMs = 1794499900000, PulseTransport? transport})
      : dir = Directory.systemTemp.createTempSync('pulse-dart-test-'),
        clock = FakeClock(startMs),
        random = SeededRandom(seed),
        fake = FakeTransport() {
    this.transport = transport ?? fake;
  }

  final Directory dir;
  final FakeClock clock;
  final SeededRandom random;
  final FakeTransport fake;
  late final PulseTransport transport;
  final List<String> log = <String>[];
  PulseOptions options = PulseOptions();
  PulseDevice device = testDevice();
  String? noBackupDir;
  PulseExitInfo? Function()? previousExit;
  PulseCore? _core;

  PulseCore get core => _core!;
  set core(PulseCore c) => _core = c;

  String get dataDir => '${dir.path}${Platform.pathSeparator}data';

  PulseCore start() {
    core = PulseCore(
      'sp_pub_local',
      'http://127.0.0.1:8787',
      options,
      device,
      PulseEnvironment(
        dataDirectory: dataDir,
        noBackupDirectory: noBackupDir,
        previousExit: previousExit,
        clock: clock,
        random: random,
        transport: transport,
        log: log.add,
      ),
    )..start();
    return core;
  }

  /// Simulates a process kill: the old core is abandoned (no quit, no close).
  PulseCore restart() => start();

  void step(int ms, [int ticks = 1]) {
    for (var i = 0; i < ticks; i++) {
      clock.advance(ms);
      core.tick();
    }
  }

  void drain([int ticks = 12]) => step(10, ticks);

  List<File> queueFiles() {
    final q = Directory('$dataDir${Platform.pathSeparator}queue');
    return q.existsSync() ? q.listSync().whereType<File>().toList() : <File>[];
  }

  void dispose() {
    _core?.close();
    try {
      dir.deleteSync(recursive: true);
    } on Object {
      // Windows may still hold a handle of an abandoned core
    }
  }
}

/// A real HTTP collector stand-in on 127.0.0.1: answers with [answer], records bodies,
/// dedupes like the collector. [answer] returns a status, or -1 to drop the connection.
class TestServer {
  TestServer._(this._server);

  final HttpServer _server;
  final FakeCollector collector = FakeCollector();
  final List<Sent> requests = <Sent>[];
  int Function(Sent s) answer = (s) => 200;
  Map<String, String> extraHeaders = <String, String>{};
  String Function(Sent s, int status)? bodyFor;

  String get url => 'http://127.0.0.1:${_server.port}';

  static Future<TestServer> start() async {
    final t = TestServer._(await HttpServer.bind(InternetAddress.loopbackIPv4, 0));
    t._server.listen(t._handle);
    return t;
  }

  Future<void> _handle(HttpRequest req) async {
    final chunks = await req.fold<BytesBuilder>(BytesBuilder(), (b, c) => b..add(c));
    final gz = req.headers.value('content-encoding') == 'gzip';
    final s = Sent(req.uri.toString(), gz, chunks.takeBytes());
    requests.add(s);
    final status = answer(s);
    if (status < 0) {
      final socket = await req.response.detachSocket(writeHeaders: false);
      socket.destroy();
      return;
    }
    if (status >= 200 && status < 300 && req.uri.path == '/v2/batch') collector.receive(s.body);
    req.response.statusCode = status;
    extraHeaders.forEach(req.response.headers.set);
    req.response.headers.contentType = ContentType.json;
    req.response.write(bodyFor?.call(s, status) ?? '{"ok":${status < 300}}');
    await req.response.close();
  }

  Future<void> close() => _server.close(force: true);
}

/// Advance the fake clock, tick, and wait (real time) for an HTTP answer to arrive.
Future<void> stepAsync(Rig rig, int ms) async {
  rig.clock.advance(ms);
  rig.core.tick();
  final until = DateTime.now().add(const Duration(seconds: 10));
  while (rig.core.waitingForResponse && DateTime.now().isBefore(until)) {
    await Future<void>.delayed(const Duration(milliseconds: 1));
  }
}

Future<void> drainAsync(Rig rig, [int ticks = 20]) async {
  for (var i = 0; i < ticks; i++) {
    await stepAsync(rig, 10);
  }
}

String repoFile(String rel) {
  var dir = Directory.current;
  for (var i = 0; i < 6; i++) {
    final f = File('${dir.path}${Platform.pathSeparator}$rel');
    if (f.existsSync()) return f.path;
    dir = dir.parent;
  }
  throw StateError('$rel not found above ${Directory.current.path}');
}
