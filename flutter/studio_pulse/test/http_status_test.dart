// Delivery over real HTTP: an in-process HttpServer on 127.0.0.1 answers with the
// collector's status codes; the SDK uses its production transport (dart:io HttpClient)
// and a fake clock. Checked in release-style code paths (no debug-only branches; the
// aptabase_flutter bug of treating a release-mode 5xx as success cannot happen here).
import 'package:flutter_test/flutter_test.dart';
import 'package:studio_pulse/core.dart';

import 'support/kit.dart';

void play(PulseCore core, int levels) {
  for (var i = 1; i <= levels; i++) {
    core
      ..levelStart(i)
      ..levelFail(i, 'out_of_moves', posX: 0.25, posY: 0.5, progress: 0.5)
      ..levelStart(i)
      ..levelComplete(i, score: 100 * i, stars: 3);
  }
}

List<int> range(int from, int to) => <int>[for (var i = from; i <= to; i++) i];

void main() {
  late TestServer server;
  late Rig rig;

  setUp(() async {
    server = await TestServer.start();
  });

  tearDown(() async {
    rig.dispose();
    await server.close();
  });

  Rig newRig() => rig = Rig(transport: IoHttpTransport(timeout: const Duration(seconds: 5)));

  PulseCore startOn(Rig r) {
    final core = PulseCore('sp_pub_local', server.url, r.options, r.device,
        PulseEnvironment(dataDirectory: r.dataDir, clock: r.clock, random: r.random, transport: r.transport, log: r.log.add))
      ..start();
    r.core = core;
    return core;
  }

  test('kill mid-flush: nothing lost, the re-sent batch is deduped', () async {
    newRig();
    var hang = true;
    server.answer = (s) => hang ? -1 : 200; // first request: the server stored it, the answer never came back
    final a = startOn(rig);
    play(a, 6);
    server.answer = (s) {
      server.collector.receive(s.body); // stored on the server side ...
      return -1; // ... but the connection dies before the answer
    };
    a.flush();
    await Future<void>.delayed(const Duration(milliseconds: 200));
    a.levelStart(7);
    hang = false;
    server.answer = (s) => 200;
    final b = startOn(rig); // the old core is abandoned like a killed process
    b.flush();
    await drainAsync(rig, 30);
    final seqs = server.collector.seqs(b.installId);
    expect(seqs, range(1, 29)); // 27 recorded before the kill + session_start + pulse_exit
    expect(server.collector.duplicates, greaterThanOrEqualTo(26));
    expect(server.collector.names(), contains('pulse_exit'));
    expect(b.stats.queued, 0);
  });

  test('offline for 24 hours, then everything is replayed in order', () async {
    newRig();
    var online = false;
    server.answer = (s) => online ? 200 : -1;
    final core = startOn(rig);
    final t0 = rig.clock.now;
    for (var h = 0; h < 24; h++) {
      if (h > 0) core.onForeground();
      for (var m = 0; m < 10; m++) {
        core.levelStart(h * 10 + m);
        await stepAsync(rig, 30000);
        await stepAsync(rig, 30000);
        core.levelComplete(h * 10 + m);
      }
      core.onBackground();
      for (var i = 0; i < 50; i++) {
        await stepAsync(rig, 60000);
      }
    }
    expect(rig.clock.now - t0, greaterThanOrEqualTo(24 * 3600000));
    expect(server.requests.length, greaterThan(500)); // kept retrying with backoff
    expect(core.stats.sent, 0);
    final queued = core.stats.queued;
    online = true;
    core.onForeground();
    for (var i = 0; i < 200 && core.stats.queued > 0; i++) {
      await stepAsync(rig, 1000);
    }
    final seqs = server.collector.seqs();
    expect(core.stats.queued, 0);
    expect(seqs, range(1, seqs.length));
    expect(seqs.length, greaterThanOrEqualTo(queued));
    expect(server.collector.rows.map((r) => r['ts']! as int).reduce((a, b) => a < b ? a : b), t0);
    expect(server.collector.named('pulse_session_start').length, 25);
  });

  test('413 halves the batch until it fits', () async {
    newRig();
    server.answer = (s) => s.events.length > 10 ? 413 : 200;
    rig.options.flushEvents = 1000;
    final core = startOn(rig);
    play(core, 10);
    core.flush();
    await drainAsync(rig, 30);
    final sizes = server.requests.map((r) => r.events.length).toList();
    expect(sizes.take(3), <int>[42, 21, 10]);
    expect(sizes.skip(2).every((n) => n <= 10), isTrue);
    expect(server.collector.seqs(), range(1, 42));
  });

  test('400 drops the batch and keeps the reason', () async {
    newRig();
    var n = 0;
    server.answer = (s) => ++n == 1 ? 400 : 200;
    server.bodyFor = (s, st) => st == 400 ? '{"ok":false,"error":"ctx.uuid:install_id"}' : '{"ok":true}';
    final core = startOn(rig);
    play(core, 1);
    core.flush();
    await drainAsync(rig);
    expect(core.stats.rejected, 6);
    expect(core.stats.lastError, contains('ctx.uuid:install_id'));
    expect(core.stats.queued, 0);
    core
      ..levelStart(2)
      ..flush();
    await drainAsync(rig);
    expect(server.collector.rows.length, 1);
  });

  test('401 disables sending until the next launch', () async {
    newRig();
    server.answer = (s) => 401;
    final core = startOn(rig);
    play(core, 1);
    core.flush();
    await drainAsync(rig);
    expect(core.stats.disabled, isTrue);
    final before = server.requests.length;
    play(core, 10);
    core.flush();
    for (var i = 0; i < 30; i++) {
      await stepAsync(rig, 120000);
    }
    expect(server.requests.length, before);
    expect(core.stats.queued, greaterThan(40));
    server.answer = (s) => 200;
    final next = startOn(rig);
    next.flush();
    await drainAsync(rig);
    expect(server.collector.rows.length, greaterThan(40));
    expect(next.stats.disabled, isFalse);
  });

  test('429 and 5xx back off exponentially with jitter, Retry-After is honoured', () async {
    newRig();
    final codes = <int>[429, 503, 500, -1, 502, 503, 503, 503, 503];
    var k = 0;
    server.answer = (s) => k < codes.length ? codes[k++] : 200;
    final core = startOn(rig);
    play(core, 1);
    core.flush();
    const base = <int>[2000, 4000, 8000, 16000, 32000, 60000, 60000, 60000, 60000];
    for (var i = 0; i < codes.length; i++) {
      while (core.waitingForResponse) {
        await Future<void>.delayed(const Duration(milliseconds: 1));
      }
      final sentAt = rig.clock.mono;
      await stepAsync(rig, 1);
      final wait = core.retryAtMs - (sentAt + 1);
      expect(wait, inInclusiveRange(base[i] + 999, base[i] + 5000), reason: 'retry ${i + 1} after ${codes[i]}');
      final before = server.requests.length;
      rig.clock.advance(wait - 2);
      core.tick();
      expect(server.requests.length, before, reason: 'no retry before the backoff ends');
      await stepAsync(rig, 2);
      expect(server.requests.length, before + 1, reason: 'retry when the backoff ends');
    }
    await drainAsync(rig);
    final seqs = server.collector.seqs();
    expect(seqs.length, greaterThanOrEqualTo(6));
    expect(seqs, range(1, seqs.length));

    // Retry-After from the server wins over a shorter backoff
    server.answer = (s) => 429;
    server.extraHeaders = <String, String>{'Retry-After': '30'};
    core
      ..screen('x')
      ..flush();
    while (core.waitingForResponse) {
      await Future<void>.delayed(const Duration(milliseconds: 1));
    }
    final sent = rig.clock.mono;
    await stepAsync(rig, 1);
    expect(core.retryAtMs - sent, greaterThanOrEqualTo(30000));
  });

  test('gzip only from 1 KB, and the server can read it', () async {
    newRig();
    rig.options.flushEvents = 1000;
    final core = startOn(rig);
    core.flush();
    await drainAsync(rig, 3);
    play(core, 5);
    core.flush();
    await drainAsync(rig, 3);
    expect(server.requests[0].gzip, isFalse);
    expect(server.requests[1].gzip, isTrue);
    expect(server.requests[1].raw.length, lessThan(server.requests[1].json.length ~/ 2));
    expect(server.collector.seqs(), range(1, 22));
  });
}
