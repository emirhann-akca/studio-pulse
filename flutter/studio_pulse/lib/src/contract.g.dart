// GENERATED from contract/pulse_v2.yaml by `python -m studio_pulse.contract.generate`. Do not edit.
//
// Event contract v2 for the Flutter SDK (docs/CONTRACT.md).

/// Contract and envelope versions.
class PulseContract {
  PulseContract._();

  static const String version = '2.0';
  static const int batchVersion = 2;
}

/// Limits.
class PulseLimits {
  PulseLimits._();

  static const int batchMaxEvents = 500;
  static const int batchMaxBytes = 1048576;
  static const int eventMaxBytes = 8192;
  static const int nameMaxLen = 40;
  static const int maxParams = 25;
  static const int stringMaxLen = 100;
  static const int futureToleranceMs = 3600000;
  static const int lateAfterMs = 2592000000;
  static const int minTsMs = 1577836800000;
  static const int sessionTimeoutMs = 1800000;
  static const int sessionMaxMs = 86400000;
  static const int errorsPerLaunch = 10;
  static const int flushEvents = 20;
  static const int flushIntervalMs = 30000;
  static const int heartbeatMs = 60000;
  static const int queueMaxEvents = 10000;
  static const int queueMaxBytes = 5242880;
  static const int retryMinMs = 2000;
  static const int retryMaxMs = 60000;
  static const int rawRetentionDays = 396;
  static const int deletionMaxDays = 90;
}

/// GA4-compatible name rules.
class PulseNames {
  PulseNames._();

  static const String pattern = '^[a-z][a-z0-9_]{0,39}\$';
  static const String sdkPrefix = 'pulse_';
  static const List<String> reservedPrefixes = <String>['firebase_', 'google_', 'ga_', 'dynamic_link_', 'notification_'];
  static const List<String> reservedNames = <String>['ad_activeview', 'ad_click', 'ad_exposure', 'ad_query', 'ad_reward', 'adunit_exposure', 'app_background', 'app_clear_data', 'app_exception', 'app_remove', 'app_store_refund', 'app_store_subscription_cancel', 'app_store_subscription_convert', 'app_store_subscription_renew', 'app_update', 'app_upgrade', 'error', 'first_open', 'first_visit', 'os_update', 'session_start', 'session_start_with_rollout', 'user_engagement'];
}

/// Event names.
class PulseEvents {
  PulseEvents._();

  static const String pulseInstall = 'pulse_install';
  static const String pulseSessionStart = 'pulse_session_start';
  static const String pulseForeground = 'pulse_foreground';
  static const String pulseBackground = 'pulse_background';
  static const String pulseHeartbeat = 'pulse_heartbeat';
  static const String pulseAppUpdate = 'pulse_app_update';
  static const String pulseError = 'pulse_error';
  static const String pulseExit = 'pulse_exit';
  static const String pulseConsent = 'pulse_consent';
  static const String pulsePerf = 'pulse_perf';
  static const String pulseLoad = 'pulse_load';
  static const String levelStart = 'level_start';
  static const String levelComplete = 'level_complete';
  static const String levelFail = 'level_fail';
  static const String levelQuit = 'level_quit';
  static const String tutorialBegin = 'tutorial_begin';
  static const String tutorialStep = 'tutorial_step';
  static const String tutorialComplete = 'tutorial_complete';
  static const String settingChange = 'setting_change';
  static const String earnVirtualCurrency = 'earn_virtual_currency';
  static const String spendVirtualCurrency = 'spend_virtual_currency';
  static const String purchase = 'purchase';
  static const String adImpression = 'ad_impression';
  static const String adEvent = 'ad_event';
  static const String screenView = 'screen_view';
}

/// Parameter names.
class PulseParams {
  PulseParams._();

  static const String installStore = 'install_store';
  static const String installerRaw = 'installer_raw';
  static const String restored = 'restored';
  static const String cold = 'cold';
  static const String sinceLastS = 'since_last_s';
  static const String fgMs = 'fg_ms';
  static const String fromVersion = 'from_version';
  static const String type = 'type';
  static const String msg = 'msg';
  static const String frame = 'frame';
  static const String fp = 'fp';
  static const String fatal = 'fatal';
  static const String reason = 'reason';
  static const String prevSid = 'prev_sid';
  static const String prevVersion = 'prev_version';
  static const String rssKb = 'rss_kb';
  static const String state = 'state';
  static const String pulseV = 'pulse_v';
  static const String tier = 'tier';
  static const String scene = 'scene';
  static const String windowS = 'window_s';
  static const String frames = 'frames';
  static const String ftSrc = 'ft_src';
  static const String ftP50Ms = 'ft_p50_ms';
  static const String ftP95Ms = 'ft_p95_ms';
  static const String ftMaxMs = 'ft_max_ms';
  static const String slowFrames = 'slow_frames';
  static const String jankyFrames = 'janky_frames';
  static const String cpuMainP95Ms = 'cpu_main_p95_ms';
  static const String gpuP95Ms = 'gpu_p95_ms';
  static const String memSrc = 'mem_src';
  static const String memAllocMb = 'mem_alloc_mb';
  static const String memReservedMb = 'mem_reserved_mb';
  static const String gcReservedMb = 'gc_reserved_mb';
  static const String sysUsedMb = 'sys_used_mb';
  static const String sysMemMb = 'sys_mem_mb';
  static const String kind = 'kind';
  static const String durationMs = 'duration_ms';
  static const String level = 'level';
  static const String levelName = 'level_name';
  static const String world = 'world';
  static const String attempt = 'attempt';
  static const String mode = 'mode';
  static const String score = 'score';
  static const String stars = 'stars';
  static const String movesLeft = 'moves_left';
  static const String progress = 'progress';
  static const String posX = 'pos_x';
  static const String posY = 'pos_y';
  static const String posSpace = 'pos_space';
  static const String tutorial = 'tutorial';
  static const String step = 'step';
  static const String stepName = 'step_name';
  static const String setting = 'setting';
  static const String from = 'from';
  static const String to = 'to';
  static const String virtualCurrencyName = 'virtual_currency_name';
  static const String value = 'value';
  static const String itemName = 'item_name';
  static const String transactionId = 'transaction_id';
  static const String productId = 'product_id';
  static const String priceMicros = 'price_micros';
  static const String currency = 'currency';
  static const String isRestore = 'is_restore';
  static const String isSandbox = 'is_sandbox';
  static const String adPlatform = 'ad_platform';
  static const String adFormat = 'ad_format';
  static const String adSource = 'ad_source';
  static const String adUnitName = 'ad_unit_name';
  static const String placement = 'placement';
  static const String precision = 'precision';
  static const String action = 'action';
  static const String errorCode = 'error_code';
  static const String screenName = 'screen_name';
  static const String screenClass = 'screen_class';
}

/// Batch context keys.
class PulseCtx {
  PulseCtx._();

  static const String appId = 'app_id';
  static const String platform = 'platform';
  static const String appVersion = 'app_version';
  static const String installId = 'install_id';
  static const String build = 'build';
  static const String buildEnv = 'build_env';
  static const String installStore = 'install_store';
  static const String installerRaw = 'installer_raw';
  static const String appName = 'app_name';
  static const String sdk = 'sdk';
  static const String engine = 'engine';
  static const String os = 'os';
  static const String deviceModel = 'device_model';
  static const String deviceTier = 'device_tier';
  static const String ramMb = 'ram_mb';
  static const String locale = 'locale';
  static const String tzOffsetMin = 'tz_offset_min';
  static const String consent = 'consent';
  static const String child = 'child';
}

/// Event keys.
class PulseFields {
  PulseFields._();

  static const String id = 'id';
  static const String seq = 'seq';
  static const String name = 'name';
  static const String ts = 'ts';
  static const String sid = 'sid';
  static const String snum = 'snum';
  static const String fgMs = 'fg_ms';
  static const String uid = 'uid';
  static const String p = 'p';
}

/// Values of `platform`.
class PulsePlatform {
  PulsePlatform._();

  static const String android = 'android';
  static const String ios = 'ios';
  static const String editor = 'editor';
  static const String windows = 'windows';
  static const String macos = 'macos';
  static const String linux = 'linux';
  static const String webgl = 'webgl';
  static const List<String> all = <String>['android', 'ios', 'editor', 'windows', 'macos', 'linux', 'webgl'];
}

/// Values of `build_env`.
class PulseBuildEnv {
  PulseBuildEnv._();

  static const String release = 'release';
  static const String debug = 'debug';
  static const String editor = 'editor';
  static const List<String> all = <String>['release', 'debug', 'editor'];
}

/// Values of `install_store`.
class PulseInstallStore {
  PulseInstallStore._();

  static const String googlePlay = 'google_play';
  static const String appStore = 'app_store';
  static const String testflight = 'testflight';
  static const String amazon = 'amazon';
  static const String samsung = 'samsung';
  static const String huawei = 'huawei';
  static const String xiaomi = 'xiaomi';
  static const String sideload = 'sideload';
  static const String simulator = 'simulator';
  static const String editor = 'editor';
  static const String unknown = 'unknown';
  static const List<String> all = <String>['google_play', 'app_store', 'testflight', 'amazon', 'samsung', 'huawei', 'xiaomi', 'sideload', 'simulator', 'editor', 'unknown'];
}

/// Values of `consent`.
class PulseConsent {
  PulseConsent._();

  static const String granted = 'granted';
  static const String denied = 'denied';
  static const String notRequired = 'not_required';
  static const String unknown = 'unknown';
  static const List<String> all = <String>['granted', 'denied', 'not_required', 'unknown'];
}

/// Values of `device_tier`.
class PulseDeviceTier {
  PulseDeviceTier._();

  static const String low = 'low';
  static const String mid = 'mid';
  static const String high = 'high';
  static const String unknown = 'unknown';
  static const List<String> all = <String>['low', 'mid', 'high', 'unknown'];
}

/// Values of `exit_reason`.
class PulseExitReason {
  PulseExitReason._();

  static const String crash = 'crash';
  static const String crashNative = 'crash_native';
  static const String anr = 'anr';
  static const String lowMemory = 'low_memory';
  static const String user = 'user';
  static const String unclean = 'unclean';
  static const String unknown = 'unknown';
  static const List<String> all = <String>['crash', 'crash_native', 'anr', 'low_memory', 'user', 'unclean', 'unknown'];
}

/// Values of `quit_reason`.
class PulseQuitReason {
  PulseQuitReason._();

  static const String menu = 'menu';
  static const String restart = 'restart';
  static const String backgroundTimeout = 'background_timeout';
  static const List<String> all = <String>['menu', 'restart', 'background_timeout'];
}

/// Values of `setting`.
class PulseSetting {
  PulseSetting._();

  static const String music = 'music';
  static const String sfx = 'sfx';
  static const String haptics = 'haptics';
  static const String notifications = 'notifications';
  static const String language = 'language';
  static const String quality = 'quality';
  static const List<String> all = <String>['music', 'sfx', 'haptics', 'notifications', 'language', 'quality'];
}

/// Values of `ad_action`.
class PulseAdAction {
  PulseAdAction._();

  static const String request = 'request';
  static const String fill = 'fill';
  static const String show = 'show';
  static const String click = 'click';
  static const String reward = 'reward';
  static const String fail = 'fail';
  static const List<String> all = <String>['request', 'fill', 'show', 'click', 'reward', 'fail'];
}

/// Values of `pos_space`.
class PulsePosSpace {
  PulsePosSpace._();

  static const String norm = 'norm';
  static const String world = 'world';
  static const List<String> all = <String>['norm', 'world'];
}

/// Values of `registry_status`.
class PulseRegistryStatus {
  PulseRegistryStatus._();

  static const String newValue = 'new';
  static const String testing = 'testing';
  static const String live = 'live';
  static const List<String> all = <String>['new', 'testing', 'live'];
}

/// Stores that count as production.
class PulseStores {
  PulseStores._();

  static const List<String> production = <String>['google_play', 'app_store', 'amazon', 'samsung', 'huawei', 'xiaomi'];
}
