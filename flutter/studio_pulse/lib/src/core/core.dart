import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import '../contract.g.dart';
import 'assign.dart';
import 'environment.dart';
import 'json_writer.dart';
import 'options.dart';
import 'queue.dart';
import 'state.dart';
import 'text.dart';

/// The SDK engine (pure Dart, no Flutter imports). It follows the Unity SDK's PulseCore
/// rule for rule; contract/golden/sdk_script.json replayed by both must give identical
/// batches (test/conformance_test.dart). The host calls [tick] regularly; HTTP answers
/// are applied on the next tick. No method throws.
class PulseCore {
  PulseCore(String writeKey, String endpoint, PulseOptions? options, PulseDevice device, this._env)
      : _opt = options ?? PulseOptions(),
        _dev = device,
        _writeKey = writeKey {
    var base = endpoint.trim();
    while (base.endsWith('/')) {
      base = base.substring(0, base.length - 1);
    }
    if (base.endsWith('/v2/batch')) base = base.substring(0, base.length - 9);
    _batchUrl = '$base/v2/batch';
    _deleteUrl = '$base/v2/delete';
    _statePath = '${_env.dataDirectory}${Platform.pathSeparator}state.json';
    _queue = PulseQueue('${_env.dataDirectory}${Platform.pathSeparator}queue', _env.log)
      ..maxEvents = _opt.queueMaxEvents < 1 ? 1 : _opt.queueMaxEvents
      ..maxBytes = _opt.queueMaxBytes < 4096 ? 4096 : _opt.queueMaxBytes
      ..segmentMaxEvents = _opt.segmentMaxEvents < 1 ? 1 : _opt.segmentMaxEvents
      ..segmentMaxBytes = _opt.segmentMaxBytes < 1024 ? 1024 : _opt.segmentMaxBytes;
    final limit = _opt.maxBatchEvents < PulseLimits.batchMaxEvents ? _opt.maxBatchEvents : PulseLimits.batchMaxEvents;
    _batchLimit = limit < 1 ? 1 : limit;
  }

  static const String version = '2.0.0';
  static const String sdkName = 'flutter/$version';
  static const Set<String> _freeTextKeys = <String>{'level_name', 'step_name', 'msg'};
  static const List<String> _catalogNames = <String>[
    PulseEvents.levelStart, PulseEvents.levelComplete, PulseEvents.levelFail, PulseEvents.levelQuit,
    PulseEvents.tutorialBegin, PulseEvents.tutorialStep, PulseEvents.tutorialComplete,
    PulseEvents.settingChange, PulseEvents.earnVirtualCurrency, PulseEvents.spendVirtualCurrency,
    PulseEvents.purchase, PulseEvents.adImpression, PulseEvents.adEvent, PulseEvents.screenView,
    PulseEvents.pulsePerf, PulseEvents.pulseLoad,
  ];

  final PulseOptions _opt;
  final PulseEnvironment _env;
  final PulseDevice _dev;
  final String _writeKey;
  late final String _batchUrl;
  late final String _deleteUrl;
  late final String _statePath;
  late final PulseQueue _queue;

  PulseState? _state;
  int _savedSeq = -1;
  late Uint8List _ctx;
  final PulseJsonWriter _ev = PulseJsonWriter();
  final PulseJsonWriter _body = PulseJsonWriter(16 * 1024);
  String _sidText = '';
  int _pcount = 0;
  int _pendingSeq = 0;
  int _evMono = 0;

  bool _started = false;
  bool _collecting = false;
  bool _pendingConsent = false;
  final List<Uint8List> _preConsent = <Uint8List>[];
  String? _uid;

  bool _foreground = false;
  int _fgAccum = 0;
  int _fgSince = 0;
  int _bgSince = 0;
  int _lastEventMono = 0;

  int _levelActive = -1;
  int _levelAttempt = 0;
  int _levelStartFg = 0;

  final Set<String> _errLaunch = <String>{};
  final Set<String> _errSession = <String>{};
  bool _inErrorHook = false;

  bool _inFlight = false;
  bool _inFlightDelete = false;
  List<QueuePart>? _parts;
  int _partsCount = 0;
  PulseHttpResult? _result;
  int _attempt = 0;
  int _retryAt = 0;
  int _drainCount = 0;
  bool _disabled = false;
  bool _noGzip = false;
  late int _batchLimit;
  int _lastFlush = 0;
  bool _queueLoaded = false;

  int _recorded = 0, _sent = 0, _rejected = 0, _requests = 0, _droppedBase = 0, _writeFailed = 0;
  int _lastStatus = 0;
  String? _lastError;

  PulseOptions get options => _opt;
  PulseClock get _clock => _env.clock;
  PulseRandom get _rnd => _env.random;

  // ---------------------------------------------------------------- start

  /// Loads the state and queue and records install / session / update / exit events.
  void start() {
    if (_started) return;
    _started = true;
    try {
      _startInner();
    } on Object catch (e) {
      _warn('start failed: $e');
    }
  }

  void _startInner() {
    final now = _clock.nowMs();
    final mono = _clock.monotonicMs();
    var prev = PulseState.load(_statePath);
    if (prev != null && prev.optedOut) {
      _state = prev;
      _collecting = false;
      return;
    }
    if (_opt.consentRequired && (prev == null || prev.consent != PulseConsent.granted)) {
      _pendingConsent = true; // memory only until setConsent(true)
      prev = null;
    }
    final maxSeq = _pendingConsent ? 0 : _loadQueue();
    if (prev != null) _droppedBase = prev.dropped;
    _collecting = true;
    _foreground = true;
    _fgSince = mono;
    _fgAccum = 0;
    _lastEventMono = mono;
    _lastFlush = mono;

    final fresh = prev == null || prev.installId == null;
    final restored = !fresh && !_markerMatches(prev.installId!);
    PulseExitInfo? exit;
    if (!fresh && !restored) exit = _previousExit(prev);
    late PulseState st;
    if (fresh || restored) {
      if (restored) _queue.wipe(); // the backup's queue belongs to the original device
      st = _newState(prev);
    } else {
      st = prev;
      if (maxSeq > st.seq) st.seq = maxSeq;
    }
    _state = st;
    st.consent = _pendingConsent ? null : (st.consent ?? prev?.consent);
    _buildCtx();

    final sinceLast = fresh || restored || prev.lastActiveMs <= 0 ? 0 : _max(0, (now - prev.lastActiveMs) ~/ 1000);
    final prevSid = prev?.sid;
    final prevVersion = prev?.appVersion;
    _newSession(now); // the state may be prev: read what the previous process left first
    if (fresh || restored) _emitInstall(restored);
    _emitSessionStart(true, sinceLast);
    if (!fresh && !restored && prevVersion != null && prevVersion != _dev.appVersion) {
      if (_begin(PulseEvents.pulseAppUpdate)) {
        _pStr('from_version', prevVersion, 50);
        _commit();
      }
    }
    if (exit != null && _begin(PulseEvents.pulseExit)) {
      _pStr('reason', exit.reason);
      _pStr('prev_sid', prevSid, 36);
      _pStr('prev_version', prevVersion, 50);
      if (exit.rssKb >= 0) _pInt('rss_kb', exit.rssKb);
      _commit();
    }
    st
      ..appVersion = _dev.appVersion
      ..launchMs = now
      ..lastActiveMs = now
      ..foreground = true
      ..cleanExit = false
      ..fatalError = false;
    _saveState();
    _writeMarker();
  }

  PulseState _newState(PulseState? keep) {
    final s = PulseState()..installId = PulseJsonWriter.uuidString(PulseUuid.v4(_rnd));
    if (keep != null) {
      s.consent = keep.consent;
      s.pendingDeletes.addAll(keep.pendingDeletes);
    }
    return s;
  }

  String? get _markerPath {
    final d = _env.noBackupDirectory;
    return d == null ? null : '$d${Platform.pathSeparator}studio-pulse.install';
  }

  bool _markerMatches(String installId) {
    final path = _markerPath;
    if (path == null) return true;
    try {
      final f = File(path);
      return f.existsSync() && f.readAsStringSync().trim() == installId;
    } on Object {
      return true; // unreadable marker: do not rotate on a guess
    }
  }

  void _writeMarker() {
    final path = _markerPath;
    final id = _state?.installId;
    if (path == null || _pendingConsent || id == null) return;
    try {
      Directory(_env.noBackupDirectory!).createSync(recursive: true);
      File(path).writeAsStringSync(id);
      _env.excludeFromBackup?.call(path);
    } on Object catch (e) {
      _warn('no-backup marker: $e');
    }
  }

  PulseExitInfo? _previousExit(PulseState prev) {
    PulseExitInfo? os;
    try {
      os = _env.previousExit?.call();
    } on Object catch (e) {
      _warn('exit reason: $e');
    }
    if (os != null && os.timestampMs > 0 && prev.launchMs > 0 && os.timestampMs < prev.launchMs) os = null;
    if (os != null &&
        (os.reason == PulseExitReason.crash ||
            os.reason == PulseExitReason.crashNative ||
            os.reason == PulseExitReason.anr ||
            os.reason == PulseExitReason.lowMemory)) {
      return os;
    }
    if (prev.fatalError) return PulseExitInfo(PulseExitReason.crash, rssKb: os?.rssKb ?? -1);
    if (prev.foreground && !prev.cleanExit) return PulseExitInfo(PulseExitReason.unclean, rssKb: os?.rssKb ?? -1);
    return null;
  }

  void _buildCtx() {
    final w = PulseJsonWriter(512)..byte(0x7B);
    w
      ..key('app_id', false)
      ..string(safeAppId(_dev.appId), 150)
      ..key('platform', true)
      ..string(_oneOf(_dev.platform, PulsePlatform.all, PulsePlatform.editor))
      ..key('app_version', true)
      ..string(_dev.appVersion.isEmpty ? '0' : _dev.appVersion, 50);
    final build = _dev.build;
    if (build != null && build.isNotEmpty) {
      w
        ..key('build', true)
        ..string(build, 50);
    }
    w
      ..key('build_env', true)
      ..string(_oneOf(_dev.buildEnv, PulseBuildEnv.all, PulseBuildEnv.release))
      ..key('install_id', true)
      ..string(_state!.installId!)
      ..key('install_store', true)
      ..string(_oneOf(_dev.installStore, PulseInstallStore.all, PulseInstallStore.unknown))
      ..key('installer_raw', true);
    final raw = _dev.installerRaw;
    if (raw == null || raw.isEmpty) {
      w.nullValue();
    } else {
      w.string(raw, 100);
    }
    void opt(String k, String? v, int max) {
      if (v != null && v.isNotEmpty) {
        w
          ..key(k, true)
          ..string(v, max);
      }
    }

    opt('app_name', _dev.appName, 100);
    w
      ..key('sdk', true)
      ..string((_dev.sdk == null || _dev.sdk!.isEmpty) ? sdkName : _dev.sdk!, 50);
    opt('engine', _dev.engine, 50);
    opt('os', _dev.os, 50);
    opt('device_model', _dev.deviceModel, 100);
    final tier = _dev.deviceTier;
    if (tier != null && tier.isNotEmpty) {
      w
        ..key('device_tier', true)
        ..string(_oneOf(tier, PulseDeviceTier.all, PulseDeviceTier.unknown));
    }
    if (_dev.ramMb >= 0) {
      w
        ..key('ram_mb', true)
        ..integer(_dev.ramMb);
    }
    opt('locale', _dev.locale, 35);
    final tz = _dev.tzOffsetMin;
    if (tz != null && tz >= -900 && tz <= 900) {
      w
        ..key('tz_offset_min', true)
        ..integer(tz);
    }
    w
      ..key('consent', true)
      ..string(_consentValue())
      ..key('child', true)
      ..boolValue(_opt.childDirected)
      ..byte(0x7D);
    _ctx = w.toBytes(); // a new list: the queue starts a new segment for it
  }

  String _consentValue() {
    if (_state?.consent == PulseConsent.granted) return PulseConsent.granted;
    return _opt.consentRequired ? PulseConsent.unknown : PulseConsent.notRequired;
  }

  static String _oneOf(String v, List<String> all, String fallback) => all.contains(v) ? v : fallback;

  static int _max(int a, int b) => a > b ? a : b;

  /// Bundle ids must match ^[A-Za-z][A-Za-z0-9_.-]{0,149}$ or the collector refuses the batch.
  static String safeAppId(String id) {
    if (id.isEmpty) return 'unknown.app';
    final sb = StringBuffer();
    for (final c in id.codeUnits) {
      final ok = (c >= 0x61 && c <= 0x7A) ||
          (c >= 0x41 && c <= 0x5A) ||
          (c >= 0x30 && c <= 0x39) ||
          c == 0x5F ||
          c == 0x2E ||
          c == 0x2D;
      sb.writeCharCode(ok ? c : 0x5F);
    }
    var s = sb.toString();
    final f = s.codeUnitAt(0);
    if (!((f >= 0x61 && f <= 0x7A) || (f >= 0x41 && f <= 0x5A))) s = 'app.$s';
    return s.length > 150 ? s.substring(0, 150) : s;
  }

  void _newSession(int now) {
    final st = _state!;
    st.snum++;
    _sidText = PulseJsonWriter.uuidString(PulseUuid.v7(now, _rnd));
    st
      ..sid = _sidText
      ..sessionStartMs = now;
    _errSession.clear();
  }

  void _emitInstall(bool restored) {
    if (!_begin(PulseEvents.pulseInstall)) return;
    _pStr('install_store', _oneOf(_dev.installStore, PulseInstallStore.all, PulseInstallStore.unknown));
    final raw = _dev.installerRaw;
    if (raw == null || raw.isEmpty) {
      _pNull('installer_raw');
    } else {
      _pStr('installer_raw', raw);
    }
    _pBool('restored', restored);
    _commit();
  }

  void _emitSessionStart(bool cold, int sinceLastS) {
    if (!_begin(PulseEvents.pulseSessionStart)) return;
    _pBool('cold', cold);
    _pInt('since_last_s', sinceLastS);
    _commit();
  }

  // ---------------------------------------------------------------- recording

  int _fgMs(int mono) => _fgAccum + (_foreground ? _max(0, mono - _fgSince) : 0);

  bool _begin(String name) {
    final st = _state;
    if (!_collecting || st == null) return false;
    final now = _clock.nowMs();
    final mono = _clock.monotonicMs();
    _evMono = mono;
    _ev
      ..clear()
      ..ascii('{"id":')
      ..uuid(PulseUuid.v7(now, _rnd));
    _pendingSeq = st.seq + 1;
    _ev
      ..ascii(',"seq":')
      ..integer(_pendingSeq)
      ..ascii(',"name":')
      ..string(name)
      ..ascii(',"ts":')
      ..integer(now)
      ..ascii(',"sid":"')
      ..ascii(_sidText)
      ..ascii('","snum":')
      ..integer(st.snum)
      ..ascii(',"fg_ms":')
      ..integer(_fgMs(mono))
      ..ascii(',"uid":');
    final uid = _uid;
    if (uid != null && !_opt.childDirected) {
      _ev.string(uid, 100);
    } else {
      _ev.nullValue();
    }
    _ev.ascii(',"p":{');
    _pcount = 0;
    return true;
  }

  void _pInt(String key, int v) {
    _ev
      ..key(key, _pcount++ > 0)
      ..integer(v);
  }

  void _pDouble(String key, double v) {
    if (v.isNaN || v.isInfinite) return;
    _ev
      ..key(key, _pcount++ > 0)
      ..doubleValue(v);
  }

  void _pBool(String key, bool v) {
    _ev
      ..key(key, _pcount++ > 0)
      ..boolValue(v);
  }

  void _pNull(String key) {
    _ev
      ..key(key, _pcount++ > 0)
      ..nullValue();
  }

  void _pStr(String key, String? v, [int maxLen = PulseLimits.stringMaxLen, bool freeText = false]) {
    if (v == null) return;
    if (freeText && _opt.childDirected) return;
    _ev
      ..key(key, _pcount++ > 0)
      ..string(v, maxLen);
  }

  void _commit() {
    _ev.ascii('}}');
    if (_ev.length > PulseLimits.eventMaxBytes) {
      _rejected++;
      _warn('event over 8 KB dropped');
      return;
    }
    _state!.seq = _pendingSeq;
    _ev.byte(0x0A);
    _lastEventMono = _evMono;
    _recorded++;
    if (_pendingConsent) {
      if (_preConsent.length < _opt.pendingConsentEvents) _preConsent.add(_ev.toBytes());
      return;
    }
    try {
      _queue.append(_ctx, _ev, _pendingSeq);
    } on Object catch (e) {
      _writeFailed++;
      _warn('queue write failed: $e');
    }
  }

  void _guard(String what, void Function() body) {
    try {
      body();
    } on Object catch (e) {
      _warn('$what: $e');
    }
  }

  // ---------------------------------------------------------------- experiments
  final Set<String> _exposures = <String>{};

  /// The variation of experiment [key] for this install: deterministic from the install id
  /// (GrowthBook's hash v2), so the warehouse can recompute it and check the split. Returns
  /// -1 when the install is outside [coverage], or before consent / while the SDK is off (show
  /// the control then). An `experiment_exposure` event is sent once per session and key.
  int experiment(String key, {int variations = 2, List<double>? weights, double coverage = 1}) {
    var out = -1;
    _guard('experiment', () {
      final id = installId;
      if (id == null || key.isEmpty || variations < 1 || variations > 32) return;
      final (variation, bucket) =
          pulseAssign(key, id, variations: variations, weights: weights, coverage: coverage);
      out = variation;
      if (!_collecting) return;
      final mark = '${sessionId ?? ''}|$key';
      if (_exposures.contains(mark)) return;
      if (_exposures.length >= 256) _exposures.clear();
      _exposures.add(mark);
      if (!_begin(PulseEvents.experimentExposure)) return;
      _pStr('experiment_key', key, 64);
      _pInt('variation', variation < 0 ? 0 : variation);
      _pBool('in_experiment', variation >= 0);
      _pDouble('hash_value', bucket);
      _commit();
    });
    return out;
  }

  // ---------------------------------------------------------------- game events

  void levelStart(int level, {String? levelName, String? world, String? mode, int attempt = 0}) => _guard('levelStart', () {
        if (!_collecting) return;
        final st = _state!;
        var a = attempt;
        if (a <= 0) a = (st.attempts[level] ?? 0) + 1;
        if (st.attempts.length >= 64 && !st.attempts.containsKey(level)) st.attempts.clear();
        st.attempts[level] = a;
        _levelActive = level;
        _levelAttempt = a;
        _levelStartFg = _fgMs(_clock.monotonicMs());
        if (!_begin(PulseEvents.levelStart)) return;
        _pInt('level', _max(0, level));
        _pStr('level_name', levelName, 100, true);
        _pStr('world', world);
        _pInt('attempt', a);
        _pStr('mode', mode);
        _commit();
      });

  (int, int) _levelEnd(int level) {
    final fg = _fgMs(_clock.monotonicMs());
    int attempt;
    int duration;
    if (_levelActive == level) {
      attempt = _levelAttempt;
      duration = _max(0, fg - _levelStartFg);
    } else {
      attempt = _state!.attempts[level] ?? 0;
      if (attempt < 1) attempt = 1;
      duration = 0;
    }
    _levelActive = -1;
    return (attempt, duration);
  }

  void levelComplete(int level, {int? score, int? stars, int? movesLeft}) => _guard('levelComplete', () {
        if (!_collecting) return;
        final (attempt, duration) = _levelEnd(level);
        _state!.attempts.remove(level);
        if (!_begin(PulseEvents.levelComplete)) return;
        _pInt('level', _max(0, level));
        _pInt('attempt', attempt);
        _pInt('duration_ms', duration);
        if (score != null) _pInt('score', score);
        if (stars != null) _pInt('stars', stars < 0 ? 0 : (stars > 5 ? 5 : stars));
        if (movesLeft != null) _pInt('moves_left', movesLeft);
        _commit();
      });

  /// [reason]: a short static code (out_of_moves, hit_spike ...), at most 40 characters.
  /// [posX]/[posY]: where the player failed; with posSpace norm (default) 0-1 of the level.
  void levelFail(int level, String reason,
          {double? posX, double? posY, double? progress, String? posSpace, int? score}) =>
      _guard('levelFail', () {
        if (!_collecting) return;
        final (attempt, duration) = _levelEnd(level);
        if (!_begin(PulseEvents.levelFail)) return;
        _pInt('level', _max(0, level));
        _pInt('attempt', attempt);
        _pInt('duration_ms', duration);
        _pStr('reason', reason.isEmpty ? 'unknown' : reason, 40);
        if (progress != null) _pDouble('progress', _clamp01(progress));
        if (posX != null) _pDouble('pos_x', posX);
        if (posY != null) _pDouble('pos_y', posY);
        if (posX != null || posY != null) {
          _pStr('pos_space', posSpace == PulsePosSpace.world ? PulsePosSpace.world : PulsePosSpace.norm);
        }
        if (score != null) _pInt('score', score);
        _commit();
      });

  /// [reason]: menu, restart or background_timeout.
  void levelQuit(int level, String reason, {double? progress, double? posX, double? posY, String? posSpace}) =>
      _guard('levelQuit', () {
        if (!_collecting) return;
        final r = PulseQuitReason.all.contains(reason) ? reason : PulseQuitReason.menu;
        final (attempt, duration) = _levelEnd(level);
        _emitLevelQuit(level, attempt, duration, r, progress, posX, posY, posSpace);
      });

  void _emitLevelQuit(int level, int attempt, int duration, String reason, double? progress, double? posX,
      double? posY, String? posSpace) {
    if (!_begin(PulseEvents.levelQuit)) return;
    _pInt('level', _max(0, level));
    _pInt('attempt', attempt);
    _pInt('duration_ms', duration);
    _pStr('reason', reason);
    if (progress != null) _pDouble('progress', _clamp01(progress));
    if (posX != null) _pDouble('pos_x', posX);
    if (posY != null) _pDouble('pos_y', posY);
    if (posX != null || posY != null) {
      _pStr('pos_space', posSpace == PulsePosSpace.world ? PulsePosSpace.world : PulsePosSpace.norm);
    }
    _commit();
  }

  static double _clamp01(double v) => v < 0 ? 0 : (v > 1 ? 1 : v);

  void tutorialBegin([String? tutorial]) => _simple(PulseEvents.tutorialBegin, 'tutorial', tutorial);

  void tutorialStep(int step, {String? stepName, String? tutorial}) => _guard('tutorialStep', () {
        if (!_begin(PulseEvents.tutorialStep)) return;
        _pInt('step', _max(0, step));
        _pStr('step_name', stepName, 100, true);
        _pStr('tutorial', tutorial);
        _commit();
      });

  void tutorialComplete([String? tutorial]) => _simple(PulseEvents.tutorialComplete, 'tutorial', tutorial);

  void _simple(String name, String key, String? value) => _guard(name, () {
        if (!_begin(name)) return;
        _pStr(key, value);
        _commit();
      });

  /// [setting]: music, sfx, haptics, notifications, language or quality (PulseSetting).
  void settingChange(String setting, String? from, String to) => _guard('settingChange', () {
        if (!PulseSetting.all.contains(setting)) {
          _rejected++;
          _warn('settingChange: unknown setting $setting');
          return;
        }
        if (!_begin(PulseEvents.settingChange)) return;
        _pStr('setting', setting);
        _pStr('from', from, 20);
        _pStr('to', to, 20);
        _commit();
      });

  /// On/off settings: from=on, to=off.
  void settingToggle(String setting, {required bool from, required bool to}) =>
      settingChange(setting, from ? 'on' : 'off', to ? 'on' : 'off');

  void earnCurrency(String currencyName, double value, {String? itemName, int? level}) =>
      _currency(PulseEvents.earnVirtualCurrency, currencyName, value, itemName, level);

  void spendCurrency(String currencyName, double value, {String? itemName, int? level}) =>
      _currency(PulseEvents.spendVirtualCurrency, currencyName, value, itemName, level);

  void _currency(String name, String currencyName, double value, String? itemName, int? level) => _guard(name, () {
        if (currencyName.isEmpty || value.isNaN || value.isInfinite) {
          _rejected++;
          return;
        }
        if (!_begin(name)) return;
        _pStr('virtual_currency_name', currencyName, 40);
        _pDouble('value', value);
        _pStr('item_name', itemName);
        if (level != null) _pInt('level', _max(0, level));
        _commit();
      });

  /// [priceMicros]: price paid in micros of [currency] (4.99 = 4990000).
  void purchase(String transactionId, String productId, int priceMicros, String currency,
          {bool? isRestore, bool? isSandbox}) =>
      _guard('purchase', () {
        if (transactionId.isEmpty || productId.isEmpty || currency.isEmpty) {
          _rejected++;
          return;
        }
        if (!_begin(PulseEvents.purchase)) return;
        _pStr('transaction_id', transactionId);
        _pStr('product_id', productId);
        _pInt('price_micros', _max(0, priceMicros));
        _pStr('currency', currency, 3);
        if (isRestore != null) _pBool('is_restore', isRestore);
        if (isSandbox != null) _pBool('is_sandbox', isSandbox);
        _commit();
      });

  /// [value]: impression revenue in [currency] (dot decimal, e.g. 0.0123).
  void adImpression(String adPlatform, String adFormat, double value, String currency,
          {String? adSource, String? adUnitName, String? placement, String? precision}) =>
      _guard('adImpression', () {
        if (adPlatform.isEmpty || adFormat.isEmpty || currency.isEmpty || value.isNaN || value.isInfinite || value < 0) {
          _rejected++;
          return;
        }
        if (!_begin(PulseEvents.adImpression)) return;
        _pStr('ad_platform', adPlatform, 40);
        _pStr('ad_format', adFormat, 40);
        _pDouble('value', value);
        _pStr('currency', currency, 3);
        _pStr('ad_source', adSource);
        _pStr('ad_unit_name', adUnitName);
        _pStr('placement', placement);
        _pStr('precision', precision, 20);
        _commit();
      });

  /// [action]: request, fill, show, click, reward or fail (PulseAdAction).
  void adEvent(String action, String adFormat, {String? placement, String? errorCode}) => _guard('adEvent', () {
        if (!PulseAdAction.all.contains(action) || adFormat.isEmpty) {
          _rejected++;
          return;
        }
        if (!_begin(PulseEvents.adEvent)) return;
        _pStr('action', action);
        _pStr('ad_format', adFormat, 40);
        _pStr('placement', placement);
        _pStr('error_code', errorCode, 40);
        _commit();
      });

  void screen(String screenName, {String? screenClass}) => _guard('screen', () {
        if (screenName.isEmpty) {
          _rejected++;
          return;
        }
        if (!_begin(PulseEvents.screenView)) return;
        _pStr('screen_name', screenName);
        _pStr('screen_class', screenClass);
        _commit();
      });

  /// A custom event: name 1-40 chars [a-z][a-z0-9_]*, not pulse_*, no Firebase-reserved
  /// name or prefix; at most 25 int / double / bool / String parameters.
  bool track(String name, [PulseEventParams? p]) {
    try {
      if (!validName(name) || name.startsWith(PulseNames.sdkPrefix)) {
        _rejected++;
        _warn("track: invalid event name '$name'");
        return false;
      }
      return _trackInner(name, p);
    } on Object catch (e) {
      _warn('track: $e');
      return false;
    }
  }

  /// v1 perf telemetry (pulse_perf / pulse_load from PulsePerf).
  bool recordPerf(String name, PulseEventParams p) {
    try {
      if (name != PulseEvents.pulsePerf && name != PulseEvents.pulseLoad) return false;
      return _trackInner(name, p);
    } on Object catch (e) {
      _warn('recordPerf: $e');
      return false;
    }
  }

  bool _trackInner(String name, PulseEventParams? p) {
    if (p != null) {
      if (p.overflow) {
        _rejected++;
        _warn('track: more than 25 parameters');
        return false;
      }
      for (final k in p.keys) {
        if (!validName(k)) {
          _rejected++;
          _warn("track: invalid parameter name '$k'");
          return false;
        }
      }
    }
    if (!_begin(name)) return false;
    final catalog = _catalogNames.contains(name);
    if (p != null) {
      for (var i = 0; i < p.length; i++) {
        final k = p.keys[i];
        final v = p.values[i];
        switch (p.kinds[i]) {
          case PulseValueKind.integer:
            _pInt(k, v as int);
          case PulseValueKind.float:
            _pDouble(k, v as double);
          case PulseValueKind.flag:
            _pBool(k, v as bool);
          case PulseValueKind.text:
            _pStr(k, v as String, PulseLimits.stringMaxLen, !catalog || _freeTextKeys.contains(k));
        }
      }
    }
    _commit();
    return true;
  }

  static bool validName(String name) {
    if (name.isEmpty || name.length > PulseLimits.nameMaxLen) return false;
    final f = name.codeUnitAt(0);
    if (f < 0x61 || f > 0x7A) return false;
    for (var i = 1; i < name.length; i++) {
      final c = name.codeUnitAt(i);
      if (!((c >= 0x61 && c <= 0x7A) || (c >= 0x30 && c <= 0x39) || c == 0x5F)) return false;
    }
    if (PulseNames.reservedNames.contains(name)) return false;
    for (final pre in PulseNames.reservedPrefixes) {
      if (name.startsWith(pre)) return false;
    }
    return true;
  }

  /// The game's own account id (never an e-mail); null to clear. Not sent in child mode.
  void setUserId(String? uid) {
    if (uid != null && (uid.contains('@') || uid.isEmpty)) {
      _warn('setUserId: refused (looks like an e-mail)');
      return;
    }
    _uid = uid;
  }

  // ---------------------------------------------------------------- errors

  /// A managed error: scrubbed, fingerprinted, at most errorsPerLaunch unique fingerprints
  /// per process and each fingerprint once per session. Never throws.
  void reportError(String? type, String? message, String? stackTrace, {bool fatal = false}) {
    if (_inErrorHook) return;
    _inErrorHook = true;
    try {
      final t = (type == null || type.isEmpty) ? 'Exception' : type;
      final scrubbed = PulseErrors.scrub(message);
      final frame = PulseErrors.topFrame(stackTrace);
      final fp = PulseErrors.fingerprint(t, frame, scrubbed);
      if (!_collecting || _errSession.contains(fp)) return;
      if (!_errLaunch.contains(fp)) {
        if (_errLaunch.length >= _opt.errorsPerLaunch) return;
        _errLaunch.add(fp);
      }
      _errSession.add(fp);
      if (!_begin(PulseEvents.pulseError)) return;
      _pStr('type', t, PulseErrors.typeMaxLen);
      if (scrubbed.isNotEmpty) _pStr('msg', scrubbed, PulseErrors.msgMaxLen, true);
      _pStr('frame', frame, PulseErrors.frameMaxLen);
      _pStr('fp', fp, 20);
      _pBool('fatal', fatal);
      _commit();
      if (fatal) {
        _state!.fatalError = true;
        _saveState();
        _queue.sync();
      }
    } on Object {
      // never let the error hook throw into the app
    } finally {
      _inErrorHook = false;
    }
  }

  // ---------------------------------------------------------------- lifecycle

  void onBackground() => _guard('onBackground', () {
        if (!_started || !_foreground) return;
        final mono = _clock.monotonicMs();
        _fgAccum += _max(0, mono - _fgSince);
        _foreground = false;
        _bgSince = mono;
        if (!_collecting) return;
        if (_begin(PulseEvents.pulseBackground)) {
          _pInt('fg_ms', _fgAccum);
          _commit();
        }
        _state!
          ..foreground = false
          ..lastActiveMs = _clock.nowMs();
        if (!_pendingConsent) {
          _saveState();
          _queue.sync();
        }
        _drainCount = _max(_drainCount, _queue.pendingEvents);
        _pump(mono);
      });

  void onForeground() => _guard('onForeground', () {
        if (!_started || _foreground) return;
        final now = _clock.nowMs();
        final mono = _clock.monotonicMs();
        final away = _max(0, mono - _bgSince);
        if (!_collecting) {
          _foreground = true;
          _fgSince = mono;
          return;
        }
        if (away > _opt.sessionTimeoutMs || now - _state!.sessionStartMs > _opt.sessionMaxMs) {
          _rollSession(now, mono, away ~/ 1000, PulseQuitReason.backgroundTimeout);
        } else {
          _foreground = true;
          _fgSince = mono;
          if (_begin(PulseEvents.pulseForeground)) {
            _pInt('fg_ms', _fgAccum);
            _commit();
          }
        }
        _state!
          ..foreground = true
          ..cleanExit = false;
        if (!_pendingConsent) _saveState();
      });

  void _rollSession(int now, int mono, int sinceLastS, String quitReason) {
    if (_levelActive >= 0) {
      final level = _levelActive;
      final (attempt, duration) = _levelEnd(level);
      _emitLevelQuit(level, attempt, duration, quitReason, null, null, null, null);
    }
    _newSession(now);
    _foreground = true;
    _fgSince = mono;
    _fgAccum = 0;
    _emitSessionStart(false, sinceLastS);
  }

  /// Clean exit (desktop). Mobile OSes usually kill apps in the background instead.
  void onQuit() => _guard('onQuit', () {
        final st = _state;
        if (!_started || st == null || !_collecting || _pendingConsent) return;
        st
          ..cleanExit = true
          ..foreground = false
          ..lastActiveMs = _clock.nowMs();
        _saveState();
        _queue.closeActive(fsync: true);
      });

  // ---------------------------------------------------------------- sending

  /// Call regularly: heartbeat, session limits, HTTP answers, flush triggers.
  void tick() => _guard('tick', () {
        if (!_started) return;
        final mono = _clock.monotonicMs();
        if (_result != null) _handleResult(mono);
        if (_collecting && _foreground && !_pendingConsent) {
          if (mono - _lastEventMono >= _opt.heartbeatMs && _begin(PulseEvents.pulseHeartbeat)) {
            _pInt('fg_ms', _fgMs(mono));
            _commit();
          }
          final now = _clock.nowMs();
          if (now - _state!.sessionStartMs > _opt.sessionMaxMs) {
            _rollSession(now, mono, 0, PulseQuitReason.backgroundTimeout);
          }
        }
        _pump(mono);
      });

  /// Send everything queued now (subject to backoff).
  void flush() => _guard('flush', () {
        if (!_started) return;
        final mono = _clock.monotonicMs();
        if (_result != null) _handleResult(mono);
        if (!_pendingConsent) _drainCount = _max(_drainCount, _queue.pendingEvents);
        _pump(mono);
      });

  void _pump(int mono) {
    if (_result != null) _handleResult(mono);
    final st = _state;
    if (_inFlight || _disabled || _env.transport == null || _pendingConsent || st == null) return;
    if (mono < _retryAt) return;
    if (st.pendingDeletes.isNotEmpty) {
      _sendDelete(st.pendingDeletes.first);
      return;
    }
    if (!_collecting) return;
    final pending = _queue.pendingEvents;
    if (pending == 0) {
      _drainCount = 0;
      return;
    }
    if (_drainCount <= 0) {
      if (pending < _opt.flushEvents && mono - _lastFlush < _opt.flushIntervalMs) return;
      _drainCount = pending;
    }
    _lastFlush = mono;
    final parts = _queue.buildBatch(_body, _clock.nowMs(), _batchLimit, _opt.maxBatchBytes);
    if (parts == null) {
      _drainCount = 0;
      return;
    }
    _parts = parts;
    _partsCount = parts.fold(0, (n, p) => n + p.count);
    try {
      _post(_batchUrl, _body.toBytes());
    } on Object catch (e) {
      _queue.complete(parts, delivered: false);
      _parts = null;
      _inFlight = false;
      _warn('send failed: $e');
    }
  }

  void _sendDelete(String installId) {
    final w = PulseJsonWriter(160)
      ..byte(0x7B)
      ..key('install_id', false)
      ..string(installId)
      ..key('app_id', true)
      ..string(safeAppId(_dev.appId), 150)
      ..key('reason', true)
      ..string('user_request')
      ..byte(0x7D);
    _inFlightDelete = true;
    _post(_deleteUrl, w.toBytes());
  }

  void _post(String url, Uint8List data) {
    var gz = data.length >= _opt.gzipMinBytes && !_noGzip;
    var payload = data;
    if (gz) {
      try {
        payload = Uint8List.fromList(gzip.encode(data));
      } on Object catch (e) {
        _noGzip = true;
        gz = false;
        payload = data;
        _warn('gzip unavailable, sending uncompressed: $e');
      }
    }
    _inFlight = true;
    _requests++;
    try {
      _env.transport!.post(url, _writeKey, payload, gz, (r) => _result = r);
    } on Object catch (e) {
      _result = PulseHttpResult(0, error: e.toString());
    }
  }

  void _handleResult(int mono) {
    final r = _result!;
    _result = null;
    _inFlight = false;
    final s = r.status;
    _lastStatus = s;
    _lastError = s >= 200 && s < 300 ? null : (r.error ?? _truncate(r.body, 200));
    final st = _state!;
    if (_inFlightDelete) {
      _inFlightDelete = false;
      if ((s >= 200 && s < 300) || s == 400) {
        st.pendingDeletes.removeAt(0);
        _attempt = 0;
        _saveState();
      } else if (s == 401 || s == 403) {
        _disabled = true;
      } else {
        _backoff(r, mono);
      }
      return;
    }
    final parts = _parts;
    _parts = null;
    if (parts == null) return;
    final n = _partsCount;
    if (s >= 200 && s < 300) {
      _persistSeq();
      _queue.complete(parts, delivered: true);
      _sent += n;
      _attempt = 0;
      _drainCount = _max(0, _drainCount - n);
      _rejected += _quarantinedCount(r.body);
    } else if (s == 413) {
      if (n > 1) {
        _queue.complete(parts, delivered: false);
        _batchLimit = _max(1, n ~/ 2);
      } else {
        _persistSeq();
        _queue.complete(parts, delivered: true);
        _rejected += n;
      }
    } else if (s == 401 || s == 403) {
      _queue.complete(parts, delivered: false);
      _disabled = true;
      _warn('collector refused the write key ($s): sending stops until the next launch');
    } else if (s >= 400 && s < 500 && s != 404 && s != 408 && s != 429) {
      // 400 (and other permanent 4xx): the batch can never succeed; drop it and count it.
      _persistSeq();
      _queue.complete(parts, delivered: true);
      _rejected += n;
      _drainCount = _max(0, _drainCount - n);
      _warn('collector refused a batch ($s): $_lastError');
    } else {
      // network error, 404, 408, 429, 5xx, 3xx: keep the batch and retry later
      _queue.complete(parts, delivered: false);
      _backoff(r, mono);
    }
  }

  void _persistSeq() {
    if (_state!.seq != _savedSeq) _saveState();
  }

  static int _quarantinedCount(String? body) {
    if (body == null || body.isEmpty) return 0;
    try {
      final o = jsonDecode(body);
      if (o is Map<String, Object?> && o['quarantined'] is int) return o['quarantined']! as int;
    } on Object {
      // not JSON
    }
    return 0;
  }

  final Uint8List _rand4 = Uint8List(4);

  void _backoff(PulseHttpResult r, int mono) {
    _attempt++;
    var delay = _opt.retryMinMs;
    for (var i = 1; i < _attempt && delay < _opt.retryMaxMs; i++) {
      delay *= 2;
    }
    if (delay > _opt.retryMaxMs) delay = _opt.retryMaxMs;
    final span = _max(0, _opt.retryJitterMaxMs - _opt.retryJitterMinMs);
    _rnd.nextBytes(_rand4, 0, 4);
    final u = _rand4[0] | (_rand4[1] << 8) | (_rand4[2] << 16) | (_rand4[3] << 24);
    delay += _opt.retryJitterMinMs + (span > 0 ? u % (span + 1) : 0);
    if (r.retryAfterSeconds > 0) {
      final ra = (r.retryAfterSeconds < 300 ? r.retryAfterSeconds : 300) * 1000;
      if (ra > delay) delay = ra;
    }
    _retryAt = mono + delay;
  }

  /// When the next retry may happen (monotonic ms), for tests.
  int get retryAtMs => _retryAt;

  /// A request is out and its answer has not arrived yet (tests drive the clock around it).
  bool get waitingForResponse => _inFlight && _result == null;

  static String? _truncate(String? s, int n) => s == null || s.length <= n ? s : s.substring(0, n);

  // ---------------------------------------------------------------- privacy

  /// Grants (true) or refuses (false) analytics consent; refusing equals optOut().
  /// Granting after an opt-out starts a new install id.
  void setConsent(bool granted) => _guard('setConsent', () {
        if (!_started) return;
        if (!granted) {
          _optOutInner();
          return;
        }
        final now = _clock.nowMs();
        final mono = _clock.monotonicMs();
        final cur = _state;
        if (cur == null || cur.optedOut || !_collecting) {
          final st = _newState(cur)..consent = PulseConsent.granted;
          _state = st;
          _loadQueue();
          _collecting = true;
          _pendingConsent = false;
          _foreground = true;
          _fgSince = mono;
          _fgAccum = 0;
          _buildCtx();
          _newSession(now);
          _emitInstall(false);
          _emitSessionStart(false, 0);
        } else if (_pendingConsent) {
          _pendingConsent = false;
          cur.consent = PulseConsent.granted;
          _buildCtx();
          if (_loadQueue() > 0) _warn('queue held events from before consent was required');
          for (final line in _preConsent) {
            _ev
              ..clear()
              ..bytes(line);
            _queue.append(_ctx, _ev, 0);
          }
          _preConsent.clear();
        } else if (cur.consent != PulseConsent.granted) {
          cur.consent = PulseConsent.granted;
          _buildCtx();
        } else {
          return;
        }
        if (_begin(PulseEvents.pulseConsent)) {
          _pStr('state', PulseConsent.granted);
          _commit();
        }
        _state!
          ..appVersion = _dev.appVersion
          ..launchMs = now
          ..foreground = _foreground;
        _saveState();
        _writeMarker();
      });

  /// Stops collection, deletes the on-device queue and keeps only the opt-out flag.
  void optOut() => _guard('optOut', _optOutInner);

  void _optOutInner() {
    _queue.wipe();
    _preConsent.clear();
    final s = PulseState()
      ..optedOut = true
      ..consent = PulseConsent.denied;
    final cur = _state;
    if (cur != null) s.pendingDeletes.addAll(cur.pendingDeletes);
    _state = s;
    _collecting = false;
    _pendingConsent = false;
    _levelActive = -1;
    _saveState();
    try {
      final m = _markerPath;
      if (m != null && File(m).existsSync()) File(m).deleteSync();
    } on Object {
      // best effort
    }
  }

  /// Asks the collector to delete everything of this install (POST /v2/delete, retried
  /// until it answers), deletes the local queue and continues under a new install id.
  void requestDataDeletion() => _guard('requestDataDeletion', () {
        final st = _state;
        if (st == null || st.installId == null || _pendingConsent) return;
        if (!st.pendingDeletes.contains(st.installId) && st.pendingDeletes.length < 16) {
          st.pendingDeletes.add(st.installId!);
        }
        _queue.wipe();
        if (_collecting) {
          _rotate();
        } else {
          _saveState();
        }
        _pump(_clock.monotonicMs());
      });

  /// A new random install id (queued events keep the old one).
  void resetInstallId() => _guard('resetInstallId', () {
        if (!_collecting || _pendingConsent || _state == null) return;
        _rotate();
      });

  void _rotate() {
    final now = _clock.nowMs();
    final mono = _clock.monotonicMs();
    final old = _state!;
    _persistSeq();
    _state = _newState(old)
      ..appVersion = _dev.appVersion
      ..launchMs = now
      ..foreground = _foreground;
    _levelActive = -1;
    _buildCtx();
    _newSession(now);
    _fgAccum = 0;
    _fgSince = mono;
    _emitInstall(false);
    _emitSessionStart(false, 0);
    _saveState();
    _writeMarker();
  }

  /// The install source learned after start (iOS AppTransaction answers asynchronously).
  void setInstallSource(String installStore, String? installerRaw) => _guard('setInstallSource', () {
        if (!PulseInstallStore.all.contains(installStore)) return;
        if (_dev.installStore == installStore && _dev.installerRaw == installerRaw) return;
        _dev
          ..installStore = installStore
          ..installerRaw = installerRaw;
        if (_state?.installId != null) _buildCtx();
      });

  /// The pseudonymous install id (for support requests), or null before consent / after opt-out.
  String? get installId => _pendingConsent ? null : _state?.installId;

  String? get sessionId => _state?.sid;

  PulseStats get stats => PulseStats(
        queued: _pendingConsent ? _preConsent.length : _queue.pendingEvents,
        recorded: _recorded,
        sent: _sent,
        dropped: _droppedTotal,
        rejected: _rejected,
        requests: _requests,
        lastStatus: _lastStatus,
        lastError: _lastError,
        disabled: _disabled,
        optedOut: _state?.optedOut ?? false,
        consentState: _pendingConsent ? PulseConsent.unknown : _consentValue(),
      );

  /// Closes the open queue file.
  void close() => _guard('close', () => _queue.closeActive(fsync: true));

  int get _droppedTotal => _droppedBase + _queue.dropped + _writeFailed;

  int _loadQueue() {
    if (_queueLoaded) return 0;
    _queueLoaded = true;
    return _queue.load();
  }

  void _saveState() {
    final st = _state;
    if (st == null || (_pendingConsent && !st.optedOut)) return;
    try {
      st.dropped = _droppedTotal;
      st.save(_statePath);
      _savedSeq = st.seq;
    } on Object catch (e) {
      _warn('state write failed: $e');
    }
  }

  void _warn(String m) {
    try {
      _env.log?.call('[studio_pulse] $m');
    } on Object {
      // logging must never fail the SDK
    }
  }
}
