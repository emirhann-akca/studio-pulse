// Pure C# (no UnityEngine): the SDK engine shared by the Unity adapter, console
// apps and tests. docs/SDK_UNITY.md describes the behaviour; the Dart SDK
// (flutter/studio_pulse) implements the same rules and the conformance test
// (contract/golden/sdk_script.json) proves both write identical batches.
//
// Threading: every public method takes one lock and never throws. The host calls
// Tick() regularly (Unity: every frame; it is a few comparisons when idle). HTTP
// answers may arrive on any thread; they are applied on the next Tick().
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;

namespace Studio.Pulse
{
    public sealed class PulseCore
    {
        public const string Version = "2.0.0";
        public const string SdkName = "unity/" + Version;

        static readonly HashSet<string> FreeTextKeys = new HashSet<string> { "level_name", "step_name", "msg" };

        readonly object _gate = new object();
        readonly PulseOptions _opt;
        readonly PulseEnvironment _env;
        readonly PulseDevice _dev;
        readonly IPulseClock _clock;
        readonly IPulseRandom _rnd;
        readonly IPulseTransport _transport;
        readonly string _writeKey;
        readonly string _batchUrl;
        readonly string _deleteUrl;
        readonly string _statePath;
        readonly PulseQueue _queue;

        PulseState _state;
        long _savedSeq = -1;
        byte[] _ctx;
        readonly PulseJsonWriter _ev = new PulseJsonWriter(1024);
        readonly PulseJsonWriter _body = new PulseJsonWriter(16 * 1024);
        readonly byte[] _uuid = new byte[16];
        readonly byte[] _sidText = new byte[36];
        readonly byte[] _rand4 = new byte[4];
        int _pcount;
        long _pendingSeq;
        long _evMono;

        bool _started;
        bool _collecting;
        bool _pendingConsent;
        readonly List<byte[]> _preConsent = new List<byte[]>();
        string _uid;

        bool _foreground;
        long _fgAccum;
        long _fgSince;
        long _bgSince;
        long _lastEventMono;

        long _levelActive = -1;
        int _levelAttempt;
        long _levelStartFg;

        readonly HashSet<string> _errLaunch = new HashSet<string>();
        readonly HashSet<string> _errSession = new HashSet<string>();
        [ThreadStatic] static bool _inErrorHook;

        bool _inFlight;
        bool _inFlightDelete;
        List<PulseQueue.Part> _parts;
        int _partsCount;
        bool _hasResult;
        PulseHttpResult _result;
        int _attempt;
        long _retryAt;
        int _drainCount; // events still to send since the last flush trigger
        bool _disabled;
        bool _noGzip;
        int _batchLimit;
        long _lastFlush;

        long _recorded, _sent, _rejected, _requests;
        long _droppedBase, _writeFailed;
        bool _queueLoaded;
        int _lastStatus;
        string _lastError;

        public PulseCore(string writeKey, string endpoint, PulseOptions options, PulseDevice device, PulseEnvironment env)
        {
            _opt = options ?? new PulseOptions();
            _env = env ?? throw new ArgumentNullException(nameof(env));
            _dev = device ?? new PulseDevice();
            _clock = env.Clock ?? new SystemClock();
            _rnd = env.Random ?? new CryptoRandom();
            _transport = env.Transport;
            _writeKey = writeKey ?? "";
            string baseUrl = (endpoint ?? "").Trim().TrimEnd('/');
            if (baseUrl.EndsWith("/v2/batch", StringComparison.Ordinal)) baseUrl = baseUrl.Substring(0, baseUrl.Length - 9);
            _batchUrl = baseUrl + "/v2/batch";
            _deleteUrl = baseUrl + "/v2/delete";
            _statePath = Path.Combine(env.DataDirectory, "state.json");
            _queue = new PulseQueue(Path.Combine(env.DataDirectory, "queue"), env.Log)
            {
                MaxEvents = Math.Max(1, _opt.QueueMaxEvents),
                MaxBytes = Math.Max(4096, _opt.QueueMaxBytes),
                SegmentMaxEvents = Math.Max(1, _opt.SegmentMaxEvents),
                SegmentMaxBytes = Math.Max(1024, _opt.SegmentMaxBytes),
            };
            _batchLimit = Math.Max(1, Math.Min(_opt.MaxBatchEvents, PulseContract.Limits.BatchMaxEvents));
        }

        public PulseOptions Options { get { return _opt; } }

        // ---------------------------------------------------------------- start

        /// <summary>Loads the state and queue and records install / session / update / exit events.</summary>
        public void Start()
        {
            lock (_gate)
            {
                if (_started) return;
                _started = true;
                try { StartLocked(); }
                catch (Exception ex) { Warn("start failed: " + ex.Message); }
            }
        }

        void StartLocked()
        {
            long now = _clock.NowMs();
            long mono = _clock.MonotonicMs();
            PulseState prev = PulseState.Load(_statePath);
            if (prev != null && prev.OptedOut)
            {
                _state = prev;
                _collecting = false;
                return;
            }
            if (_opt.ConsentRequired && (prev == null || prev.Consent != PulseContract.Consent.Granted))
            {
                _pendingConsent = true; // memory only until SetConsent(true)
                prev = null;
            }
            long maxSeq = _pendingConsent ? 0 : LoadQueue();
            if (prev != null) _droppedBase = prev.Dropped;
            _collecting = true;
            _foreground = true;
            _fgSince = mono;
            _fgAccum = 0;
            _lastEventMono = mono;
            _lastFlush = mono;

            bool fresh = prev == null || prev.InstallId == null;
            bool restored = !fresh && !MarkerMatches(prev.InstallId);
            PulseExitInfo exit = null;
            if (!fresh && !restored) exit = PreviousExit(prev);
            if (fresh || restored)
            {
                if (restored) _queue.Wipe(); // the backup's queue belongs to the original device
                _state = NewState(prev);
            }
            else
            {
                _state = prev;
                if (maxSeq > _state.Seq) _state.Seq = maxSeq;
            }
            _state.Consent = _pendingConsent ? null : (_state.Consent ?? (prev != null ? prev.Consent : null));
            BuildCtx();

            long sinceLast = fresh || restored || prev.LastActiveMs <= 0 ? 0 : Math.Max(0, (now - prev.LastActiveMs) / 1000);
            string prevSid = prev != null ? prev.Sid : null;
            string prevVersion = prev != null ? prev.AppVersion : null;
            NewSession(now); // _state may be prev: read what the previous process left first
            if (fresh || restored) EmitInstall(restored);
            EmitSessionStart(true, sinceLast);
            if (!fresh && !restored && prevVersion != null && prevVersion != _dev.AppVersion)
            {
                if (Begin(PulseContract.Events.PulseAppUpdate)) { PStr("from_version", prevVersion, 50); Commit(); }
            }
            if (exit != null)
            {
                if (Begin(PulseContract.Events.PulseExit))
                {
                    PStr("reason", exit.Reason);
                    PStr("prev_sid", prevSid, 36);
                    PStr("prev_version", prevVersion, 50);
                    if (exit.RssKb >= 0) PLong("rss_kb", exit.RssKb);
                    Commit();
                }
            }
            _state.AppVersion = _dev.AppVersion;
            _state.LaunchMs = now;
            _state.LastActiveMs = now;
            _state.Foreground = true;
            _state.CleanExit = false;
            _state.FatalError = false;
            SaveState();
            WriteMarker();
        }

        PulseState NewState(PulseState keep)
        {
            var s = new PulseState();
            PulseUuid.V4(_rnd, _uuid, 0);
            s.InstallId = PulseJsonWriter.UuidString(_uuid, 0);
            if (keep != null)
            {
                s.Consent = keep.Consent;
                s.PendingDeletes.AddRange(keep.PendingDeletes);
            }
            return s;
        }

        string MarkerPath
        {
            get { return _env.NoBackupDirectory == null ? null : Path.Combine(_env.NoBackupDirectory, "studio-pulse.install"); }
        }

        bool MarkerMatches(string installId)
        {
            string path = MarkerPath;
            if (path == null) return true;
            try
            {
                return File.Exists(path) && File.ReadAllText(path).Trim() == installId;
            }
            catch (Exception)
            {
                return true; // unreadable marker: do not rotate on a guess
            }
        }

        void WriteMarker()
        {
            string path = MarkerPath;
            if (path == null || _pendingConsent || _state.InstallId == null) return;
            try
            {
                Directory.CreateDirectory(_env.NoBackupDirectory);
                File.WriteAllText(path, _state.InstallId);
                if (_env.ExcludeFromBackup != null) _env.ExcludeFromBackup(path);
            }
            catch (Exception ex)
            {
                Warn("no-backup marker: " + ex.Message);
            }
        }

        PulseExitInfo PreviousExit(PulseState prev)
        {
            PulseExitInfo os = null;
            try { if (_env.PreviousExit != null) os = _env.PreviousExit(); }
            catch (Exception ex) { Warn("exit reason: " + ex.Message); }
            if (os != null && os.TimestampMs > 0 && prev.LaunchMs > 0 && os.TimestampMs < prev.LaunchMs) os = null; // an older process
            if (os != null)
            {
                switch (os.Reason)
                {
                    case PulseContract.ExitReason.Crash:
                    case PulseContract.ExitReason.CrashNative:
                    case PulseContract.ExitReason.Anr:
                    case PulseContract.ExitReason.LowMemory:
                        return os;
                }
            }
            if (prev.FatalError) return new PulseExitInfo { Reason = PulseContract.ExitReason.Crash, RssKb = os != null ? os.RssKb : -1 };
            if (prev.Foreground && !prev.CleanExit)
            {
                return new PulseExitInfo { Reason = PulseContract.ExitReason.Unclean, RssKb = os != null ? os.RssKb : -1 };
            }
            return null;
        }

        void BuildCtx()
        {
            var w = new PulseJsonWriter(512);
            w.Byte((byte)'{');
            w.Key("app_id", false); w.String(SafeAppId(_dev.AppId), 150);
            w.Key("platform", true); w.String(OneOf(_dev.Platform, PulseContract.Platform.All, PulseContract.Platform.Editor));
            w.Key("app_version", true); w.String(string.IsNullOrEmpty(_dev.AppVersion) ? "0" : _dev.AppVersion, 50);
            if (!string.IsNullOrEmpty(_dev.Build)) { w.Key("build", true); w.String(_dev.Build, 50); }
            w.Key("build_env", true); w.String(OneOf(_dev.BuildEnv, PulseContract.BuildEnv.All, PulseContract.BuildEnv.Release));
            w.Key("install_id", true); w.String(_state.InstallId);
            w.Key("install_store", true); w.String(OneOf(_dev.InstallStore, PulseContract.InstallStore.All, PulseContract.InstallStore.Unknown));
            w.Key("installer_raw", true);
            if (string.IsNullOrEmpty(_dev.InstallerRaw)) w.Null(); else w.String(_dev.InstallerRaw, 100);
            if (!string.IsNullOrEmpty(_dev.AppName)) { w.Key("app_name", true); w.String(_dev.AppName, 100); }
            w.Key("sdk", true); w.String(string.IsNullOrEmpty(_dev.Sdk) ? SdkName : _dev.Sdk, 50);
            if (!string.IsNullOrEmpty(_dev.Engine)) { w.Key("engine", true); w.String(_dev.Engine, 50); }
            if (!string.IsNullOrEmpty(_dev.Os)) { w.Key("os", true); w.String(_dev.Os, 50); }
            if (!string.IsNullOrEmpty(_dev.DeviceModel)) { w.Key("device_model", true); w.String(_dev.DeviceModel, 100); }
            if (!string.IsNullOrEmpty(_dev.DeviceTier)) { w.Key("device_tier", true); w.String(OneOf(_dev.DeviceTier, PulseContract.DeviceTier.All, PulseContract.DeviceTier.Unknown)); }
            if (_dev.RamMb >= 0) { w.Key("ram_mb", true); w.Long(_dev.RamMb); }
            if (!string.IsNullOrEmpty(_dev.Locale)) { w.Key("locale", true); w.String(_dev.Locale, 35); }
            if (_dev.TzOffsetMin >= -900 && _dev.TzOffsetMin <= 900) { w.Key("tz_offset_min", true); w.Long(_dev.TzOffsetMin); }
            w.Key("consent", true); w.String(ConsentValue());
            w.Key("child", true); w.Bool(_opt.ChildDirected);
            w.Byte((byte)'}');
            var ctx = new byte[w.Length];
            Buffer.BlockCopy(w.Buffer, 0, ctx, 0, w.Length);
            _ctx = ctx; // a new array: the queue starts a new segment for it
        }

        string ConsentValue()
        {
            if (_state != null && _state.Consent == PulseContract.Consent.Granted) return PulseContract.Consent.Granted;
            return _opt.ConsentRequired ? PulseContract.Consent.Unknown : PulseContract.Consent.NotRequired;
        }

        static string OneOf(string v, string[] all, string fallback)
        {
            return Array.IndexOf(all, v) >= 0 ? v : fallback;
        }

        /// <summary>Bundle ids must match ^[A-Za-z][A-Za-z0-9_.-]{0,149}$ or the collector refuses the batch.</summary>
        public static string SafeAppId(string id)
        {
            if (string.IsNullOrEmpty(id)) return "unknown.app";
            var c = id.ToCharArray();
            for (int i = 0; i < c.Length; i++)
            {
                char x = c[i];
                bool ok = (x >= 'a' && x <= 'z') || (x >= 'A' && x <= 'Z') || (x >= '0' && x <= '9') || x == '_' || x == '.' || x == '-';
                if (!ok) c[i] = '_';
            }
            string s = new string(c);
            if (!((s[0] >= 'a' && s[0] <= 'z') || (s[0] >= 'A' && s[0] <= 'Z'))) s = "app." + s;
            return s.Length > 150 ? s.Substring(0, 150) : s;
        }

        void NewSession(long now)
        {
            _state.Snum++;
            PulseUuid.V7(now, _rnd, _uuid, 0);
            PulseJsonWriter.UuidText(_uuid, 0, _sidText, 0);
            _state.Sid = PulseJsonWriter.UuidString(_uuid, 0);
            _state.SessionStartMs = now;
            _errSession.Clear();
        }

        void EmitInstall(bool restored)
        {
            if (!Begin(PulseContract.Events.PulseInstall)) return;
            PStr("install_store", OneOf(_dev.InstallStore, PulseContract.InstallStore.All, PulseContract.InstallStore.Unknown));
            if (string.IsNullOrEmpty(_dev.InstallerRaw)) PNull("installer_raw"); else PStr("installer_raw", _dev.InstallerRaw);
            PBool("restored", restored);
            Commit();
        }

        void EmitSessionStart(bool cold, long sinceLastS)
        {
            if (!Begin(PulseContract.Events.PulseSessionStart)) return;
            PBool("cold", cold);
            PLong("since_last_s", sinceLastS);
            Commit();
        }

        // ---------------------------------------------------------------- recording

        long FgMs(long mono)
        {
            return _fgAccum + (_foreground ? Math.Max(0, mono - _fgSince) : 0);
        }

        bool Begin(string name)
        {
            if (!_collecting || _state == null) return false;
            long now = _clock.NowMs();
            long mono = _clock.MonotonicMs();
            _evMono = mono;
            _ev.Clear();
            PulseUuid.V7(now, _rnd, _uuid, 0);
            _ev.Ascii("{\"id\":");
            _ev.Uuid(_uuid, 0);
            _pendingSeq = _state.Seq + 1;
            _ev.Ascii(",\"seq\":");
            _ev.Long(_pendingSeq);
            _ev.Ascii(",\"name\":");
            _ev.String(name);
            _ev.Ascii(",\"ts\":");
            _ev.Long(now);
            _ev.Ascii(",\"sid\":\"");
            _ev.Bytes(_sidText, 0, 36);
            _ev.Ascii("\",\"snum\":");
            _ev.Long(_state.Snum);
            _ev.Ascii(",\"fg_ms\":");
            _ev.Long(FgMs(mono));
            _ev.Ascii(",\"uid\":");
            if (_uid != null && !_opt.ChildDirected) _ev.String(_uid, 100); else _ev.Null();
            _ev.Ascii(",\"p\":{");
            _pcount = 0;
            return true;
        }

        void PLong(string key, long v)
        {
            _ev.Key(key, _pcount++ > 0);
            _ev.Long(v);
        }

        void PDouble(string key, double v)
        {
            if (double.IsNaN(v) || double.IsInfinity(v)) return;
            _ev.Key(key, _pcount++ > 0);
            _ev.Double(v);
        }

        void PBool(string key, bool v)
        {
            _ev.Key(key, _pcount++ > 0);
            _ev.Bool(v);
        }

        void PNull(string key)
        {
            _ev.Key(key, _pcount++ > 0);
            _ev.Null();
        }

        void PStr(string key, string v, int maxLen = PulseContract.Limits.StringMaxLen, bool freeText = false)
        {
            if (v == null) return;
            if (freeText && _opt.ChildDirected) return;
            _ev.Key(key, _pcount++ > 0);
            _ev.String(v, maxLen);
        }

        void Commit()
        {
            _ev.Ascii("}}");
            if (_ev.Length > PulseContract.Limits.EventMaxBytes)
            {
                _rejected++;
                Warn("event over 8 KB dropped");
                return;
            }
            _state.Seq = _pendingSeq;
            _ev.Byte((byte)'\n');
            _lastEventMono = _evMono;
            _recorded++;
            if (_pendingConsent)
            {
                if (_preConsent.Count < _opt.PendingConsentEvents)
                {
                    var copy = new byte[_ev.Length];
                    Buffer.BlockCopy(_ev.Buffer, 0, copy, 0, _ev.Length);
                    _preConsent.Add(copy);
                }
                return;
            }
            try
            {
                _queue.Append(_ctx, _ev, _pendingSeq);
            }
            catch (Exception ex)
            {
                _writeFailed++;
                Warn("queue write failed: " + ex.Message);
            }
        }

        // ---------------------------------------------------------------- public API: game events

        public void LevelStart(long level, string levelName = null, string world = null, string mode = null, int attempt = 0)
        {
            lock (_gate)
            {
                try
                {
                    if (!_collecting) return;
                    if (attempt <= 0)
                    {
                        _state.Attempts.TryGetValue(level, out int a);
                        attempt = a + 1;
                    }
                    if (_state.Attempts.Count >= 64 && !_state.Attempts.ContainsKey(level)) _state.Attempts.Clear();
                    _state.Attempts[level] = attempt;
                    _levelActive = level;
                    _levelAttempt = attempt;
                    _levelStartFg = FgMs(_clock.MonotonicMs());
                    if (!Begin(PulseContract.Events.LevelStart)) return;
                    PLong("level", Math.Max(0, level));
                    PStr("level_name", levelName, 100, true);
                    PStr("world", world);
                    PLong("attempt", attempt);
                    PStr("mode", mode);
                    Commit();
                }
                catch (Exception ex) { Warn("LevelStart: " + ex.Message); }
            }
        }

        void LevelEnd(long level, out int attempt, out long durationMs)
        {
            long fg = FgMs(_clock.MonotonicMs());
            if (_levelActive == level)
            {
                attempt = _levelAttempt;
                durationMs = Math.Max(0, fg - _levelStartFg);
            }
            else
            {
                _state.Attempts.TryGetValue(level, out attempt);
                if (attempt < 1) attempt = 1;
                durationMs = 0;
            }
            _levelActive = -1;
        }

        public void LevelComplete(long level, long? score = null, int? stars = null, long? movesLeft = null)
        {
            lock (_gate)
            {
                try
                {
                    if (!_collecting) return;
                    LevelEnd(level, out int attempt, out long duration);
                    _state.Attempts.Remove(level);
                    if (!Begin(PulseContract.Events.LevelComplete)) return;
                    PLong("level", Math.Max(0, level));
                    PLong("attempt", attempt);
                    PLong("duration_ms", duration);
                    if (score.HasValue) PLong("score", score.Value);
                    if (stars.HasValue) PLong("stars", Math.Max(0, Math.Min(5, stars.Value)));
                    if (movesLeft.HasValue) PLong("moves_left", movesLeft.Value);
                    Commit();
                }
                catch (Exception ex) { Warn("LevelComplete: " + ex.Message); }
            }
        }

        /// <param name="reason">A short static code (out_of_moves, hit_spike ...), at most 40 characters.</param>
        /// <param name="posX">Where the player failed; with posSpace "norm" (default) 0-1 of the level bounds.</param>
        /// <param name="progress">Share of the level done, 0-1.</param>
        public void LevelFail(long level, string reason, double? posX = null, double? posY = null, double? progress = null, string posSpace = null, long? score = null)
        {
            lock (_gate)
            {
                try
                {
                    if (!_collecting) return;
                    LevelEnd(level, out int attempt, out long duration);
                    if (!Begin(PulseContract.Events.LevelFail)) return;
                    PLong("level", Math.Max(0, level));
                    PLong("attempt", attempt);
                    PLong("duration_ms", duration);
                    PStr("reason", string.IsNullOrEmpty(reason) ? "unknown" : reason, 40);
                    if (progress.HasValue) PDouble("progress", Clamp01(progress.Value));
                    if (posX.HasValue) PDouble("pos_x", posX.Value);
                    if (posY.HasValue) PDouble("pos_y", posY.Value);
                    if (posX.HasValue || posY.HasValue) PStr("pos_space", posSpace == PulseContract.PosSpace.World ? PulseContract.PosSpace.World : PulseContract.PosSpace.Norm);
                    if (score.HasValue) PLong("score", score.Value);
                    Commit();
                }
                catch (Exception ex) { Warn("LevelFail: " + ex.Message); }
            }
        }

        /// <param name="reason">menu, restart or background_timeout (PulseContract.QuitReason).</param>
        public void LevelQuit(long level, string reason, double? progress = null, double? posX = null, double? posY = null, string posSpace = null)
        {
            lock (_gate)
            {
                try
                {
                    if (!_collecting) return;
                    if (Array.IndexOf(PulseContract.QuitReason.All, reason) < 0) reason = PulseContract.QuitReason.Menu;
                    LevelEnd(level, out int attempt, out long duration);
                    EmitLevelQuit(level, attempt, duration, reason, progress, posX, posY, posSpace);
                }
                catch (Exception ex) { Warn("LevelQuit: " + ex.Message); }
            }
        }

        void EmitLevelQuit(long level, int attempt, long duration, string reason, double? progress, double? posX, double? posY, string posSpace)
        {
            if (!Begin(PulseContract.Events.LevelQuit)) return;
            PLong("level", Math.Max(0, level));
            PLong("attempt", attempt);
            PLong("duration_ms", duration);
            PStr("reason", reason);
            if (progress.HasValue) PDouble("progress", Clamp01(progress.Value));
            if (posX.HasValue) PDouble("pos_x", posX.Value);
            if (posY.HasValue) PDouble("pos_y", posY.Value);
            if (posX.HasValue || posY.HasValue) PStr("pos_space", posSpace == PulseContract.PosSpace.World ? PulseContract.PosSpace.World : PulseContract.PosSpace.Norm);
            Commit();
        }

        static double Clamp01(double v)
        {
            return v < 0 ? 0 : (v > 1 ? 1 : v);
        }

        public void TutorialBegin(string tutorial = null)
        {
            Simple(PulseContract.Events.TutorialBegin, "tutorial", tutorial);
        }

        public void TutorialStep(long step, string stepName = null, string tutorial = null)
        {
            lock (_gate)
            {
                try
                {
                    if (!Begin(PulseContract.Events.TutorialStep)) return;
                    PLong("step", Math.Max(0, step));
                    PStr("step_name", stepName, 100, true);
                    PStr("tutorial", tutorial);
                    Commit();
                }
                catch (Exception ex) { Warn("TutorialStep: " + ex.Message); }
            }
        }

        public void TutorialComplete(string tutorial = null)
        {
            Simple(PulseContract.Events.TutorialComplete, "tutorial", tutorial);
        }

        void Simple(string name, string key, string value)
        {
            lock (_gate)
            {
                try
                {
                    if (!Begin(name)) return;
                    PStr(key, value);
                    Commit();
                }
                catch (Exception ex) { Warn(name + ": " + ex.Message); }
            }
        }

        /// <param name="setting">music, sfx, haptics, notifications, language or quality (PulseContract.Setting).</param>
        public void SettingChange(string setting, string from, string to)
        {
            lock (_gate)
            {
                try
                {
                    if (Array.IndexOf(PulseContract.Setting.All, setting) < 0 || to == null)
                    {
                        _rejected++;
                        Warn("SettingChange: unknown setting " + setting);
                        return;
                    }
                    if (!Begin(PulseContract.Events.SettingChange)) return;
                    PStr("setting", setting);
                    PStr("from", from, 20);
                    PStr("to", to, 20);
                    Commit();
                }
                catch (Exception ex) { Warn("SettingChange: " + ex.Message); }
            }
        }

        public void SettingChange(string setting, bool from, bool to)
        {
            SettingChange(setting, from ? "on" : "off", to ? "on" : "off");
        }

        public void EarnCurrency(string currencyName, double value, string itemName = null, long? level = null)
        {
            Currency(PulseContract.Events.EarnVirtualCurrency, currencyName, value, itemName, level);
        }

        public void SpendCurrency(string currencyName, double value, string itemName = null, long? level = null)
        {
            Currency(PulseContract.Events.SpendVirtualCurrency, currencyName, value, itemName, level);
        }

        void Currency(string name, string currencyName, double value, string itemName, long? level)
        {
            lock (_gate)
            {
                try
                {
                    if (string.IsNullOrEmpty(currencyName) || double.IsNaN(value) || double.IsInfinity(value)) { _rejected++; return; }
                    if (!Begin(name)) return;
                    PStr("virtual_currency_name", currencyName, 40);
                    PDouble("value", value);
                    PStr("item_name", itemName);
                    if (level.HasValue) PLong("level", Math.Max(0, level.Value));
                    Commit();
                }
                catch (Exception ex) { Warn(name + ": " + ex.Message); }
            }
        }

        /// <param name="priceMicros">Price paid in micros of <paramref name="currency"/> (4.99 TRY = 4990000).</param>
        public void Purchase(string transactionId, string productId, long priceMicros, string currency, bool? isRestore = null, bool? isSandbox = null)
        {
            lock (_gate)
            {
                try
                {
                    if (string.IsNullOrEmpty(transactionId) || string.IsNullOrEmpty(productId) || string.IsNullOrEmpty(currency)) { _rejected++; return; }
                    if (!Begin(PulseContract.Events.Purchase)) return;
                    PStr("transaction_id", transactionId);
                    PStr("product_id", productId);
                    PLong("price_micros", Math.Max(0, priceMicros));
                    PStr("currency", currency, 3);
                    if (isRestore.HasValue) PBool("is_restore", isRestore.Value);
                    if (isSandbox.HasValue) PBool("is_sandbox", isSandbox.Value);
                    Commit();
                }
                catch (Exception ex) { Warn("Purchase: " + ex.Message); }
            }
        }

        /// <param name="value">Impression revenue in <paramref name="currency"/> (dot decimal, e.g. 0.0123).</param>
        public void AdImpression(string adPlatform, string adFormat, double value, string currency, string adSource = null, string adUnitName = null, string placement = null, string precision = null)
        {
            lock (_gate)
            {
                try
                {
                    if (string.IsNullOrEmpty(adPlatform) || string.IsNullOrEmpty(adFormat) || string.IsNullOrEmpty(currency)
                        || double.IsNaN(value) || double.IsInfinity(value) || value < 0) { _rejected++; return; }
                    if (!Begin(PulseContract.Events.AdImpression)) return;
                    PStr("ad_platform", adPlatform, 40);
                    PStr("ad_format", adFormat, 40);
                    PDouble("value", value);
                    PStr("currency", currency, 3);
                    PStr("ad_source", adSource);
                    PStr("ad_unit_name", adUnitName);
                    PStr("placement", placement);
                    PStr("precision", precision, 20);
                    Commit();
                }
                catch (Exception ex) { Warn("AdImpression: " + ex.Message); }
            }
        }

        /// <param name="action">request, fill, show, click, reward or fail (PulseContract.AdAction).</param>
        public void AdEvent(string action, string adFormat, string placement = null, string errorCode = null)
        {
            lock (_gate)
            {
                try
                {
                    if (Array.IndexOf(PulseContract.AdAction.All, action) < 0 || string.IsNullOrEmpty(adFormat)) { _rejected++; return; }
                    if (!Begin(PulseContract.Events.AdEvent)) return;
                    PStr("action", action);
                    PStr("ad_format", adFormat, 40);
                    PStr("placement", placement);
                    PStr("error_code", errorCode, 40);
                    Commit();
                }
                catch (Exception ex) { Warn("AdEvent: " + ex.Message); }
            }
        }

        public void Screen(string screenName, string screenClass = null)
        {
            lock (_gate)
            {
                try
                {
                    if (string.IsNullOrEmpty(screenName)) { _rejected++; return; }
                    if (!Begin(PulseContract.Events.ScreenView)) return;
                    PStr("screen_name", screenName);
                    PStr("screen_class", screenClass);
                    Commit();
                }
                catch (Exception ex) { Warn("Screen: " + ex.Message); }
            }
        }

        /// <summary>
        /// A custom event: name 1-40 chars [a-z][a-z0-9_]*, not pulse_*, no Firebase-reserved name
        /// or prefix; at most 25 int / double / bool / string parameters. Keep the set of
        /// names small and static (no ids or timestamps in names).
        /// </summary>
        public bool Track(string name, PulseParams p = null)
        {
            lock (_gate)
            {
                try
                {
                    if (!ValidName(name) || name.StartsWith(PulseContract.Names.SdkPrefix, StringComparison.Ordinal))
                    {
                        _rejected++;
                        Warn("Track: invalid event name '" + name + "'");
                        return false;
                    }
                    return TrackLocked(name, p);
                }
                catch (Exception ex) { Warn("Track: " + ex.Message); return false; }
            }
        }

        /// <summary>v1 perf telemetry (pulse_perf / pulse_load from the perf sampler and load timer).</summary>
        public bool RecordPerf(string name, PulseParams p)
        {
            lock (_gate)
            {
                try
                {
                    if (name != PulseContract.Events.PulsePerf && name != PulseContract.Events.PulseLoad) return false;
                    return TrackLocked(name, p);
                }
                catch (Exception ex) { Warn("RecordPerf: " + ex.Message); return false; }
            }
        }

        bool TrackLocked(string name, PulseParams p)
        {
            if (p != null)
            {
                if (p.Overflow) { _rejected++; Warn("Track: more than 25 parameters"); return false; }
                for (int i = 0; i < p.Count; i++)
                {
                    if (!ValidName(p.Keys[i])) { _rejected++; Warn("Track: invalid parameter name '" + p.Keys[i] + "'"); return false; }
                }
            }
            if (!Begin(name)) return false;
            bool catalog = Array.IndexOf(CatalogNames, name) >= 0;
            if (p != null)
            {
                for (int i = 0; i < p.Count; i++)
                {
                    switch (p.Kinds[i])
                    {
                        case PulseParams.Kind.Long: PLong(p.Keys[i], p.Longs[i]); break;
                        case PulseParams.Kind.Double: PDouble(p.Keys[i], p.Doubles[i]); break;
                        case PulseParams.Kind.Bool: PBool(p.Keys[i], p.Longs[i] != 0); break;
                        default:
                            bool free = !catalog || FreeTextKeys.Contains(p.Keys[i]);
                            PStr(p.Keys[i], p.Strings[i], PulseContract.Limits.StringMaxLen, free);
                            break;
                    }
                }
            }
            Commit();
            return true;
        }

        static readonly string[] CatalogNames =
        {
            PulseContract.Events.LevelStart, PulseContract.Events.LevelComplete, PulseContract.Events.LevelFail,
            PulseContract.Events.LevelQuit, PulseContract.Events.TutorialBegin, PulseContract.Events.TutorialStep,
            PulseContract.Events.TutorialComplete, PulseContract.Events.SettingChange, PulseContract.Events.EarnVirtualCurrency,
            PulseContract.Events.SpendVirtualCurrency, PulseContract.Events.Purchase, PulseContract.Events.AdImpression,
            PulseContract.Events.AdEvent, PulseContract.Events.ScreenView, PulseContract.Events.PulsePerf, PulseContract.Events.PulseLoad,
        };

        public static bool ValidName(string name)
        {
            if (string.IsNullOrEmpty(name) || name.Length > PulseContract.Limits.NameMaxLen) return false;
            if (name[0] < 'a' || name[0] > 'z') return false;
            for (int i = 1; i < name.Length; i++)
            {
                char c = name[i];
                if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_')) return false;
            }
            if (Array.IndexOf(PulseContract.Names.ReservedNames, name) >= 0) return false;
            foreach (string pre in PulseContract.Names.ReservedPrefixes)
            {
                if (name.StartsWith(pre, StringComparison.Ordinal)) return false;
            }
            return true;
        }

        /// <summary>The game's own account id (never an e-mail); null to clear. Not sent in child mode.</summary>
        public void SetUserId(string uid)
        {
            lock (_gate)
            {
                if (uid != null && (uid.IndexOf('@') >= 0 || uid.Length == 0)) { Warn("SetUserId: refused (looks like an e-mail)"); return; }
                _uid = uid;
            }
        }

        // ---------------------------------------------------------------- errors

        /// <summary>
        /// A managed error: scrubbed, fingerprinted, at most ErrorsPerLaunch unique fingerprints
        /// per process and each fingerprint once per session. Safe from any thread, never throws.
        /// </summary>
        public void ReportError(string type, string message, string stackTrace, bool fatal = false)
        {
            if (_inErrorHook) return;
            _inErrorHook = true;
            try
            {
                string t = string.IsNullOrEmpty(type) ? "Exception" : type;
                string scrubbed = PulseErrors.Scrub(message);
                string frame = PulseErrors.TopFrame(stackTrace);
                string fp = PulseErrors.Fingerprint(t, frame, scrubbed);
                lock (_gate)
                {
                    if (!_collecting || _errSession.Contains(fp)) return;
                    if (!_errLaunch.Contains(fp))
                    {
                        if (_errLaunch.Count >= _opt.ErrorsPerLaunch) return;
                        _errLaunch.Add(fp);
                    }
                    _errSession.Add(fp);
                    if (!Begin(PulseContract.Events.PulseError)) return;
                    PStr("type", t, PulseErrors.TypeMaxLen);
                    if (scrubbed.Length > 0) PStr("msg", scrubbed, PulseErrors.MsgMaxLen, true);
                    PStr("frame", frame, PulseErrors.FrameMaxLen);
                    PStr("fp", fp, 20);
                    PBool("fatal", fatal);
                    Commit();
                    if (fatal)
                    {
                        _state.FatalError = true;
                        SaveState();
                        _queue.Sync();
                    }
                }
            }
            catch (Exception)
            {
                // never let the error hook throw into the game
            }
            finally
            {
                _inErrorHook = false;
            }
        }

        // ---------------------------------------------------------------- lifecycle

        public void OnBackground()
        {
            lock (_gate)
            {
                try
                {
                    if (!_started || !_foreground) return;
                    long mono = _clock.MonotonicMs();
                    _fgAccum += Math.Max(0, mono - _fgSince);
                    _foreground = false;
                    _bgSince = mono;
                    if (!_collecting) return;
                    if (Begin(PulseContract.Events.PulseBackground)) { PLong("fg_ms", _fgAccum); Commit(); }
                    _state.Foreground = false;
                    _state.LastActiveMs = _clock.NowMs();
                    if (!_pendingConsent)
                    {
                        SaveState();
                        _queue.Sync();
                    }
                    _drainCount = Math.Max(_drainCount, _queue.PendingEvents);
                    PumpLocked(mono);
                }
                catch (Exception ex) { Warn("OnBackground: " + ex.Message); }
            }
        }

        public void OnForeground()
        {
            lock (_gate)
            {
                try
                {
                    if (!_started || _foreground) return;
                    long now = _clock.NowMs();
                    long mono = _clock.MonotonicMs();
                    long away = Math.Max(0, mono - _bgSince);
                    if (!_collecting)
                    {
                        _foreground = true;
                        _fgSince = mono;
                        return;
                    }
                    if (away > _opt.SessionTimeoutMs || now - _state.SessionStartMs > _opt.SessionMaxMs)
                    {
                        RollSession(now, mono, away / 1000, PulseContract.QuitReason.BackgroundTimeout);
                    }
                    else
                    {
                        _foreground = true;
                        _fgSince = mono;
                        if (Begin(PulseContract.Events.PulseForeground)) { PLong("fg_ms", _fgAccum); Commit(); }
                    }
                    _state.Foreground = true;
                    _state.CleanExit = false;
                    if (!_pendingConsent) SaveState();
                }
                catch (Exception ex) { Warn("OnForeground: " + ex.Message); }
            }
        }

        /// <summary>A new session: a level left open is closed as level_quit first (in the old session).</summary>
        void RollSession(long now, long mono, long sinceLastS, string quitReason)
        {
            if (_levelActive >= 0)
            {
                long level = _levelActive;
                LevelEnd(level, out int attempt, out long duration);
                EmitLevelQuit(level, attempt, duration, quitReason, null, null, null, null);
            }
            NewSession(now);
            _foreground = true;
            _fgSince = mono;
            _fgAccum = 0;
            EmitSessionStart(false, sinceLastS);
        }

        /// <summary>Clean exit (desktop quit). Mobile OSes usually kill apps in the background instead.</summary>
        public void OnQuit()
        {
            lock (_gate)
            {
                try
                {
                    if (!_started || _state == null || !_collecting || _pendingConsent) return;
                    _state.CleanExit = true;
                    _state.Foreground = false;
                    _state.LastActiveMs = _clock.NowMs();
                    SaveState();
                    _queue.CloseActive(true);
                }
                catch (Exception ex) { Warn("OnQuit: " + ex.Message); }
            }
        }

        // ---------------------------------------------------------------- sending

        /// <summary>Call regularly (every frame is fine): heartbeat, session limits, HTTP answers, flush triggers.</summary>
        public void Tick()
        {
            lock (_gate)
            {
                try
                {
                    if (!_started) return;
                    long mono = _clock.MonotonicMs();
                    if (_hasResult) HandleResult(mono);
                    if (_collecting && _foreground && !_pendingConsent)
                    {
                        if (mono - _lastEventMono >= _opt.HeartbeatMs)
                        {
                            if (Begin(PulseContract.Events.PulseHeartbeat)) { PLong("fg_ms", FgMs(mono)); Commit(); }
                        }
                        long now = _clock.NowMs();
                        if (now - _state.SessionStartMs > _opt.SessionMaxMs) RollSession(now, mono, 0, PulseContract.QuitReason.BackgroundTimeout);
                    }
                    PumpLocked(mono);
                }
                catch (Exception ex) { Warn("Tick: " + ex.Message); }
            }
        }

        /// <summary>Send everything queued now (subject to backoff).</summary>
        public void Flush()
        {
            lock (_gate)
            {
                try
                {
                    if (!_started) return;
                    long mono = _clock.MonotonicMs();
                    if (_hasResult) HandleResult(mono);
                    if (!_pendingConsent) _drainCount = Math.Max(_drainCount, _queue.PendingEvents);
                    PumpLocked(mono);
                }
                catch (Exception ex) { Warn("Flush: " + ex.Message); }
            }
        }

        void PumpLocked(long mono)
        {
            if (_hasResult) HandleResult(mono);
            if (_inFlight || _disabled || _transport == null || _pendingConsent || _state == null) return;
            if (mono < _retryAt) return;
            if (_state.PendingDeletes.Count > 0)
            {
                SendDelete(_state.PendingDeletes[0]);
                return;
            }
            if (!_collecting) return;
            int pending = _queue.PendingEvents;
            if (pending == 0) { _drainCount = 0; return; }
            if (_drainCount <= 0)
            {
                if (pending < _opt.FlushEvents && mono - _lastFlush < _opt.FlushIntervalMs) return;
                _drainCount = pending;
            }
            _lastFlush = mono;
            List<PulseQueue.Part> parts = _queue.BuildBatch(_body, _clock.NowMs(), _batchLimit, _opt.MaxBatchBytes);
            if (parts == null) { _drainCount = 0; return; }
            _parts = parts;
            _partsCount = 0;
            foreach (var p in parts) _partsCount += p.Count;
            try
            {
                Post(_batchUrl, _body.Buffer, _body.Length);
            }
            catch (Exception ex)
            {
                _queue.Complete(parts, false);
                _parts = null;
                _inFlight = false;
                Warn("send failed: " + ex.Message);
            }
        }

        void SendDelete(string installId)
        {
            var w = new PulseJsonWriter(160);
            w.Byte((byte)'{');
            w.Key("install_id", false); w.String(installId);
            w.Key("app_id", true); w.String(SafeAppId(_dev.AppId), 150);
            w.Key("reason", true); w.String("user_request");
            w.Byte((byte)'}');
            _inFlightDelete = true;
            Post(_deleteUrl, w.Buffer, w.Length);
        }

        void Post(string url, byte[] data, int length)
        {
            bool gzip = length >= _opt.GzipMinBytes && !_noGzip;
            byte[] payload = data;
            int len = length;
            if (gzip)
            {
                try
                {
                    using (var ms = new MemoryStream(length / 3 + 64))
                    {
                        using (var gz = new GZipStream(ms, CompressionLevel.Fastest, true)) gz.Write(data, 0, length);
                        payload = ms.ToArray();
                        len = payload.Length;
                    }
                }
                catch (Exception ex)
                {
                    // e.g. a runtime without the native zlib helper: send plain JSON from now on
                    _noGzip = true;
                    gzip = false;
                    payload = data;
                    len = length;
                    Warn("gzip unavailable, sending uncompressed: " + ex.Message);
                }
            }
            _inFlight = true;
            _requests++;
            try
            {
                _transport.Post(url, _writeKey, payload, len, gzip, OnHttpResult);
            }
            catch (Exception ex)
            {
                OnHttpResult(new PulseHttpResult { Status = 0, RetryAfterSeconds = -1, Error = ex.Message });
            }
        }

        void OnHttpResult(PulseHttpResult r)
        {
            lock (_gate)
            {
                _result = r;
                _hasResult = true;
            }
        }

        void HandleResult(long mono)
        {
            _hasResult = false;
            _inFlight = false;
            PulseHttpResult r = _result;
            int s = r.Status;
            _lastStatus = s;
            _lastError = s >= 200 && s < 300 ? null : (r.Error ?? Truncate(r.Body, 200));
            if (_inFlightDelete)
            {
                _inFlightDelete = false;
                if ((s >= 200 && s < 300) || s == 400)
                {
                    _state.PendingDeletes.RemoveAt(0);
                    _attempt = 0;
                    SaveState();
                }
                else if (s == 401 || s == 403) _disabled = true;
                else Backoff(r, mono);
                return;
            }
            List<PulseQueue.Part> parts = _parts;
            _parts = null;
            if (parts == null) return;
            int n = _partsCount;
            if (s >= 200 && s < 300)
            {
                PersistSeq();
                _queue.Complete(parts, true);
                _sent += n;
                _attempt = 0;
                _drainCount = Math.Max(0, _drainCount - n);
                int quarantined = QuarantinedCount(r.Body);
                if (quarantined > 0) _rejected += quarantined;
            }
            else if (s == 413)
            {
                if (n > 1)
                {
                    _queue.Complete(parts, false);
                    _batchLimit = Math.Max(1, n / 2);
                }
                else
                {
                    PersistSeq();
                    _queue.Complete(parts, true);
                    _rejected += n;
                }
            }
            else if (s == 401 || s == 403)
            {
                _queue.Complete(parts, false);
                _disabled = true;
                Warn("collector refused the write key (" + s + "): sending stops until the next launch");
            }
            else if (s >= 400 && s < 500 && s != 404 && s != 408 && s != 429)
            {
                // 400 (and other permanent 4xx): the batch can never succeed; drop it and count it.
                PersistSeq();
                _queue.Complete(parts, true);
                _rejected += n;
                _drainCount = Math.Max(0, _drainCount - n);
                Warn("collector refused a batch (" + s + "): " + _lastError);
            }
            else
            {
                // network error, 404, 408, 429, 5xx, 3xx: keep the batch and retry later
                _queue.Complete(parts, false);
                Backoff(r, mono);
            }
        }

        void PersistSeq()
        {
            if (_state.Seq != _savedSeq) SaveState();
        }

        static int QuarantinedCount(string body)
        {
            if (string.IsNullOrEmpty(body)) return 0;
            try
            {
                if (MiniJson.Parse(body) is Dictionary<string, object> o) return (int)MiniJson.Long(o, "quarantined");
            }
            catch (Exception)
            {
            }
            return 0;
        }

        void Backoff(PulseHttpResult r, long mono)
        {
            _attempt++;
            long delay = _opt.RetryMinMs;
            for (int i = 1; i < _attempt && delay < _opt.RetryMaxMs; i++) delay *= 2;
            if (delay > _opt.RetryMaxMs) delay = _opt.RetryMaxMs;
            int span = Math.Max(0, _opt.RetryJitterMaxMs - _opt.RetryJitterMinMs);
            _rnd.NextBytes(_rand4, 0, 4);
            uint u = (uint)(_rand4[0] | (_rand4[1] << 8) | (_rand4[2] << 16) | (_rand4[3] << 24));
            delay += _opt.RetryJitterMinMs + (span > 0 ? (long)(u % (uint)(span + 1)) : 0);
            if (r.RetryAfterSeconds > 0) delay = Math.Max(delay, Math.Min(300L, r.RetryAfterSeconds) * 1000L);
            _retryAt = mono + delay;
        }

        /// <summary>When the next retry may happen (monotonic ms), for tests.</summary>
        public long RetryAtMs { get { lock (_gate) return _retryAt; } }

        static string Truncate(string s, int n)
        {
            return s == null || s.Length <= n ? s : s.Substring(0, n);
        }

        // ---------------------------------------------------------------- privacy

        /// <summary>
        /// Grants (true) or refuses (false) analytics consent. Refusing equals OptOut().
        /// Granting after an opt-out starts a new install id.
        /// </summary>
        public void SetConsent(bool granted)
        {
            lock (_gate)
            {
                try
                {
                    if (!_started) return;
                    if (!granted) { OptOutLocked(); return; }
                    long now = _clock.NowMs();
                    long mono = _clock.MonotonicMs();
                    if (_state == null || _state.OptedOut || !_collecting)
                    {
                        _state = NewState(_state);
                        _state.Consent = PulseContract.Consent.Granted;
                        LoadQueue();
                        _collecting = true;
                        _pendingConsent = false;
                        _foreground = true;
                        _fgSince = mono;
                        _fgAccum = 0;
                        BuildCtx();
                        NewSession(now);
                        EmitInstall(false);
                        EmitSessionStart(false, 0);
                    }
                    else if (_pendingConsent)
                    {
                        _pendingConsent = false;
                        _state.Consent = PulseContract.Consent.Granted;
                        BuildCtx();
                        if (LoadQueue() > 0) Warn("queue held events from before consent was required");
                        foreach (byte[] line in _preConsent)
                        {
                            _ev.Clear();
                            _ev.Bytes(line, 0, line.Length);
                            _queue.Append(_ctx, _ev, 0);
                        }
                        _preConsent.Clear();
                    }
                    else if (_state.Consent != PulseContract.Consent.Granted)
                    {
                        _state.Consent = PulseContract.Consent.Granted;
                        BuildCtx();
                    }
                    else return;
                    if (Begin(PulseContract.Events.PulseConsent)) { PStr("state", PulseContract.Consent.Granted); Commit(); }
                    _state.AppVersion = _dev.AppVersion;
                    _state.LaunchMs = now;
                    _state.Foreground = _foreground;
                    SaveState();
                    WriteMarker();
                }
                catch (Exception ex) { Warn("SetConsent: " + ex.Message); }
            }
        }

        /// <summary>Stops collection, deletes the on-device queue and keeps only the opt-out flag.</summary>
        public void OptOut()
        {
            lock (_gate)
            {
                try { OptOutLocked(); }
                catch (Exception ex) { Warn("OptOut: " + ex.Message); }
            }
        }

        void OptOutLocked()
        {
            _queue.Wipe();
            _preConsent.Clear();
            var s = new PulseState { OptedOut = true, Consent = PulseContract.Consent.Denied };
            if (_state != null) s.PendingDeletes.AddRange(_state.PendingDeletes);
            _state = s;
            _collecting = false;
            _pendingConsent = false;
            _levelActive = -1;
            SaveState();
            try { if (MarkerPath != null && File.Exists(MarkerPath)) File.Delete(MarkerPath); }
            catch (Exception) { }
        }

        /// <summary>
        /// Asks the collector to delete everything of this install (POST /v2/delete, retried until it
        /// answers), deletes the local queue and continues under a new install id.
        /// </summary>
        public void RequestDataDeletion()
        {
            lock (_gate)
            {
                try
                {
                    if (_state == null || _state.InstallId == null || _pendingConsent) return;
                    if (!_state.PendingDeletes.Contains(_state.InstallId) && _state.PendingDeletes.Count < 16)
                    {
                        _state.PendingDeletes.Add(_state.InstallId);
                    }
                    _queue.Wipe();
                    if (_collecting) RotateLocked();
                    else SaveState();
                    PumpLocked(_clock.MonotonicMs());
                }
                catch (Exception ex) { Warn("RequestDataDeletion: " + ex.Message); }
            }
        }

        /// <summary>A new random install id (queued events keep the old one).</summary>
        public void ResetInstallId()
        {
            lock (_gate)
            {
                try
                {
                    if (!_collecting || _pendingConsent || _state == null) return;
                    RotateLocked();
                }
                catch (Exception ex) { Warn("ResetInstallId: " + ex.Message); }
            }
        }

        void RotateLocked()
        {
            long now = _clock.NowMs();
            long mono = _clock.MonotonicMs();
            PulseState old = _state;
            PersistSeq();
            _state = NewState(old);
            _state.AppVersion = _dev.AppVersion;
            _state.LaunchMs = now;
            _state.Foreground = _foreground;
            _levelActive = -1;
            BuildCtx();
            NewSession(now);
            _fgAccum = 0;
            _fgSince = mono;
            EmitInstall(false);
            EmitSessionStart(false, 0);
            SaveState();
            WriteMarker();
        }

        /// <summary>
        /// The install source learned after start (iOS AppTransaction answers asynchronously).
        /// Events from now on carry it; already queued events keep the old context.
        /// </summary>
        public void SetInstallSource(string installStore, string installerRaw)
        {
            lock (_gate)
            {
                try
                {
                    if (Array.IndexOf(PulseContract.InstallStore.All, installStore) < 0) return;
                    if (_dev.InstallStore == installStore && _dev.InstallerRaw == installerRaw) return;
                    _dev.InstallStore = installStore;
                    _dev.InstallerRaw = installerRaw;
                    if (_state != null && _state.InstallId != null) BuildCtx();
                }
                catch (Exception ex) { Warn("SetInstallSource: " + ex.Message); }
            }
        }

        /// <summary>The pseudonymous install id (for support requests), or null before consent / after opt-out.</summary>
        public string GetInstallId()
        {
            lock (_gate) return _pendingConsent || _state == null ? null : _state.InstallId;
        }

        public PulseStats GetStats()
        {
            lock (_gate)
            {
                return new PulseStats
                {
                    Queued = _pendingConsent ? _preConsent.Count : _queue.PendingEvents,
                    Recorded = _recorded,
                    Sent = _sent,
                    Dropped = DroppedTotal(),
                    Rejected = _rejected,
                    Requests = _requests,
                    LastStatus = _lastStatus,
                    LastError = _lastError,
                    Disabled = _disabled,
                    OptedOut = _state != null && _state.OptedOut,
                    ConsentState = _pendingConsent ? PulseContract.Consent.Unknown : ConsentValue(),
                };
            }
        }

        public string SessionId { get { lock (_gate) return _state == null ? null : _state.Sid; } }

        /// <summary>Closes the open queue file (tests; the Unity adapter on quit).</summary>
        public void Close()
        {
            lock (_gate)
            {
                try { _queue.CloseActive(true); }
                catch (Exception ex) { Warn("Close: " + ex.Message); }
            }
        }

        void SaveState()
        {
            if (_state == null || (_pendingConsent && !_state.OptedOut)) return;
            try
            {
                _state.Dropped = DroppedTotal();
                _state.Save(_statePath);
                _savedSeq = _state.Seq;
            }
            catch (Exception ex)
            {
                Warn("state write failed: " + ex.Message);
            }
        }

        long DroppedTotal()
        {
            return _droppedBase + _queue.Dropped + _writeFailed;
        }

        long LoadQueue()
        {
            if (_queueLoaded) return 0;
            _queueLoaded = true;
            return _queue.Load();
        }

        void Warn(string msg)
        {
            try { if (_env.Log != null) _env.Log("[Studio.Pulse] " + msg); }
            catch (Exception) { }
        }
    }
}
