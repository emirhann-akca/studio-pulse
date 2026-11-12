using System;
using System.Collections.Generic;
using UnityEngine;

namespace Studio.Pulse
{
    /// <summary>The analytics SDK facade (docs/SDK_UNITY.md). Every call is a no-op before Init and never throws.</summary>
    public static partial class Pulse
    {
        static PulseCore _core;
        static UnityWebRequestTransport _transport;
        static bool _hooked;
        static float _appTxUntil;
        static float _appTxNext;

        /// <summary>
        /// Starts the SDK: loads the on-device queue, records install / session / update / exit
        /// events, hooks exceptions and the app lifecycle, and starts the perf sampler.
        /// Call once, as early as possible (e.g. RuntimeInitializeOnLoadMethod or the first scene).
        /// </summary>
        /// <param name="writeKey">The studio's public write key (sp_pub_...).</param>
        /// <param name="endpoint">The collector base URL, e.g. https://collector.example.com.</param>
        public static void Init(string writeKey, string endpoint, PulseOptions options = null, PulseConfig perf = null)
        {
            if (_core != null) return;
            try
            {
                options = options ?? new PulseOptions();
                _transport = new UnityWebRequestTransport();
                var env = new PulseEnvironment
                {
                    DataDirectory = UnityPulsePlatform.DataDirectory,
                    Clock = new SystemClock(),
                    Random = new CryptoRandom(),
                    Transport = _transport,
                    Log = m => Debug.LogWarning(m),
                };
#if UNITY_ANDROID && !UNITY_EDITOR
                env.NoBackupDirectory = UnityPulsePlatform.AndroidNoBackupDirectory();
                env.PreviousExit = UnityPulsePlatform.AndroidPreviousExit;
#elif UNITY_IOS && !UNITY_EDITOR
                // iOS has no no-backup directory: the marker lives next to the state and is
                // excluded from iCloud / device backups, so a restored copy lacks it.
                env.NoBackupDirectory = UnityPulsePlatform.DataDirectory;
                env.ExcludeFromBackup = path => UnityEngine.iOS.Device.SetNoBackupFlag(path);
                _appTxUntil = Time.realtimeSinceStartup + 60f;
#endif
                _core = new PulseCore(writeKey, endpoint, options, UnityPulsePlatform.Device(), env);
                _core.Start();
                if (options.CaptureErrors && !_hooked)
                {
                    _hooked = true;
                    Application.logMessageReceivedThreaded += OnLog;
                }
                Initialize(perf ?? new PulseConfig(), new PulseSdkSink());
            }
            catch (Exception ex)
            {
                Debug.LogWarning("[Studio.Pulse] Init failed: " + ex.Message);
            }
        }

        /// <summary>The SDK core (null before Init): for tests and advanced use.</summary>
        public static PulseCore Core { get { return _core; } }

        static void OnLog(string condition, string stackTrace, LogType type)
        {
            if (type != LogType.Exception || _core == null || condition == null) return;
            if (condition.StartsWith("[Studio.Pulse]", StringComparison.Ordinal)) return;
            // "NullReferenceException: Object reference not set ..."
            string errType = "Exception", message = condition;
            int colon = condition.IndexOf(": ", StringComparison.Ordinal);
            if (colon > 0 && colon < 100 && condition.IndexOf(' ', 0, colon) < 0)
            {
                errType = condition.Substring(0, colon);
                message = condition.Substring(colon + 2);
            }
            _core.ReportError(errType, message, stackTrace, false);
        }

        static void PollAppTransaction()
        {
            if (_appTxUntil <= 0f) return;
            float now = Time.realtimeSinceStartup;
            if (now < _appTxNext) return;
            _appTxNext = now + 2f;
            string store = UnityPulsePlatform.IosAppTransactionStore();
            if (store != null)
            {
                _core.SetInstallSource(store, null);
                _appTxUntil = 0f;
            }
            else if (now > _appTxUntil)
            {
                _appTxUntil = 0f;
            }
        }

        // ---------------------------------------------------------------- game events

        public static void LevelStart(long level, string levelName = null, string world = null, string mode = null)
        {
            if (_core != null) _core.LevelStart(level, levelName, world, mode);
        }

        public static void LevelComplete(long level, long? score = null, int? stars = null, long? movesLeft = null)
        {
            if (_core != null) _core.LevelComplete(level, score, stars, movesLeft);
        }

        /// <param name="reason">A short static code: out_of_moves, out_of_time, hit_spike ...</param>
        /// <param name="progress">Share of the level done, 0-1 (negative: not sent).</param>
        public static void LevelFail(long level, string reason, float progress = -1f, long? score = null)
        {
            if (_core != null) _core.LevelFail(level, reason, null, null, Opt(progress), null, score);
        }

        /// <param name="position">Where the player failed: 0-1 of the level bounds, or world units with worldSpace.</param>
        public static void LevelFail(long level, string reason, Vector2 position, float progress = -1f, bool worldSpace = false, long? score = null)
        {
            if (_core != null)
            {
                _core.LevelFail(level, reason, Round(position.x), Round(position.y), Opt(progress),
                    worldSpace ? PulseContract.PosSpace.World : PulseContract.PosSpace.Norm, score);
            }
        }

        /// <param name="reason">menu or restart (background_timeout is sent by the SDK itself).</param>
        public static void LevelQuit(long level, string reason = PulseContract.QuitReason.Menu, float progress = -1f)
        {
            if (_core != null) _core.LevelQuit(level, reason, Opt(progress));
        }

        public static void LevelQuit(long level, string reason, Vector2 position, float progress = -1f, bool worldSpace = false)
        {
            if (_core != null)
            {
                _core.LevelQuit(level, reason, Opt(progress), Round(position.x), Round(position.y),
                    worldSpace ? PulseContract.PosSpace.World : PulseContract.PosSpace.Norm);
            }
        }

        public static void TutorialBegin(string tutorial = null)
        {
            if (_core != null) _core.TutorialBegin(tutorial);
        }

        public static void TutorialStep(long step, string stepName = null, string tutorial = null)
        {
            if (_core != null) _core.TutorialStep(step, stepName, tutorial);
        }

        public static void TutorialComplete(string tutorial = null)
        {
            if (_core != null) _core.TutorialComplete(tutorial);
        }

        /// <param name="setting">PulseContract.Setting: music, sfx, haptics, notifications, language, quality.</param>
        public static void SettingChange(string setting, string from, string to)
        {
            if (_core != null) _core.SettingChange(setting, from, to);
        }

        /// <summary>On/off settings: SettingChange(PulseContract.Setting.Music, true, false) sends from=on, to=off.</summary>
        public static void SettingChange(string setting, bool from, bool to)
        {
            if (_core != null) _core.SettingChange(setting, from, to);
        }

        public static void EarnCurrency(string currency, double amount, string item = null, long? level = null)
        {
            if (_core != null) _core.EarnCurrency(currency, amount, item, level);
        }

        public static void SpendCurrency(string currency, double amount, string item = null, long? level = null)
        {
            if (_core != null) _core.SpendCurrency(currency, amount, item, level);
        }

        /// <param name="priceMicros">Price paid in micros (4.99 = 4990000).</param>
        public static void Purchase(string transactionId, string productId, long priceMicros, string currency, bool? isRestore = null, bool? isSandbox = null)
        {
            if (_core != null) _core.Purchase(transactionId, productId, priceMicros, currency, isRestore, isSandbox);
        }

        /// <summary>Unity IAP style: localizedPrice (decimal) and isoCurrencyCode.</summary>
        public static void Purchase(string transactionId, string productId, decimal price, string currency, bool? isRestore = null, bool? isSandbox = null)
        {
            if (_core != null) _core.Purchase(transactionId, productId, (long)Math.Round(price * 1_000_000m), currency, isRestore, isSandbox);
        }

        /// <param name="value">Impression revenue, e.g. MAX's adInfo.Revenue or LevelPlay's impressionData.revenue.</param>
        public static void AdImpression(string adPlatform, string adFormat, double value, string currency = "USD",
            string adSource = null, string adUnitName = null, string placement = null, string precision = null)
        {
            if (_core != null) _core.AdImpression(adPlatform, adFormat, value, currency, adSource, adUnitName, placement, precision);
        }

        /// <param name="action">PulseContract.AdAction: request, fill, show, click, reward, fail.</param>
        public static void AdEvent(string action, string adFormat, string placement = null, string errorCode = null)
        {
            if (_core != null) _core.AdEvent(action, adFormat, placement, errorCode);
        }

        public static void Screen(string screenName, string screenClass = null)
        {
            if (_core != null) _core.Screen(screenName, screenClass);
        }

        /// <summary>A custom event (name rules in docs/SDK_UNITY.md). Reuse one PulseParams to allocate nothing.</summary>
        public static bool Track(string name, PulseParams parameters = null)
        {
            return _core != null && _core.Track(name, parameters);
        }

        /// <summary>A handled exception you want to see (managed exceptions Unity logs are captured already).</summary>
        public static void ReportException(Exception ex, bool fatal = false)
        {
            if (_core != null && ex != null) _core.ReportError(ex.GetType().Name, ex.Message, ex.StackTrace, fatal);
        }

        // ---------------------------------------------------------------- privacy

        public static void SetUserId(string gameAccountId)
        {
            if (_core != null) _core.SetUserId(gameAccountId);
        }

        public static void SetConsent(bool granted)
        {
            if (_core != null) _core.SetConsent(granted);
        }

        public static void OptOut()
        {
            if (_core != null) _core.OptOut();
        }

        public static void RequestDataDeletion()
        {
            if (_core != null) _core.RequestDataDeletion();
        }

        public static void ResetInstallId()
        {
            if (_core != null) _core.ResetInstallId();
        }

        public static string GetInstallId()
        {
            return _core != null ? _core.GetInstallId() : null;
        }

        public static PulseStats Stats
        {
            get { return _core != null ? _core.GetStats() : default(PulseStats); }
        }

        static double? Opt(float v)
        {
            return v < 0f || float.IsNaN(v) ? (double?)null : Round(v);
        }

        /// <summary>floats -> doubles rounded to 5 decimals: 0.42f is sent as 0.42, not 0.41999998688697815.</summary>
        static double Round(float v)
        {
            return Math.Round((double)v, 5);
        }

        /// <summary>Forwards the perf sampler's pulse_perf / pulse_load events into the SDK queue.</summary>
        sealed class PulseSdkSink : IPulseSink
        {
            readonly PulseParams _p = new PulseParams();

            public void Send(IReadOnlyList<PulseEvent> batch, PulseContext context)
            {
                if (_core == null) return;
                for (int i = 0; i < batch.Count; i++)
                {
                    PulseEvent e = batch[i];
                    _p.Clear();
                    for (int j = 0; j < e.Params.Count; j++)
                    {
                        PulseParam q = e.Params[j];
                        switch (q.Kind)
                        {
                            case PulseParamKind.Int: _p.Add(q.Key, (long)q.Number); break;
                            case PulseParamKind.Double: _p.Add(q.Key, Math.Round(q.Number, 3)); break;
                            default: _p.Add(q.Key, q.Text); break;
                        }
                    }
                    _core.RecordPerf(e.Name, _p);
                }
            }

            public void Flush()
            {
            }
        }
    }
}
