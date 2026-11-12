"""GENERATED from contract/pulse_v2.yaml by `python -m studio_pulse.contract.generate`. Do not edit.

Names, enum values and limits of event contract v2 (docs/CONTRACT.md).
"""

CONTRACT_VERSION = "2.0"
BATCH_VERSION = 2

# Limits
LIMIT_BATCH_MAX_EVENTS = 500
LIMIT_BATCH_MAX_BYTES = 1048576
LIMIT_EVENT_MAX_BYTES = 8192
LIMIT_NAME_MAX_LEN = 40
LIMIT_MAX_PARAMS = 25
LIMIT_STRING_MAX_LEN = 100
LIMIT_FUTURE_TOLERANCE_MS = 3600000
LIMIT_LATE_AFTER_MS = 2592000000
LIMIT_MIN_TS_MS = 1577836800000
LIMIT_SESSION_TIMEOUT_MS = 1800000
LIMIT_SESSION_MAX_MS = 86400000
LIMIT_ERRORS_PER_LAUNCH = 10
LIMIT_FLUSH_EVENTS = 20
LIMIT_FLUSH_INTERVAL_MS = 30000
LIMIT_HEARTBEAT_MS = 60000
LIMIT_QUEUE_MAX_EVENTS = 10000
LIMIT_QUEUE_MAX_BYTES = 5242880
LIMIT_RETRY_MIN_MS = 2000
LIMIT_RETRY_MAX_MS = 60000
LIMIT_RAW_RETENTION_DAYS = 396
LIMIT_DELETION_MAX_DAYS = 90

# Name rules
NAME_PATTERN = "^[a-z][a-z0-9_]{0,39}$"
SDK_PREFIX = "pulse_"
RESERVED_PREFIXES = (
    "firebase_",
    "google_",
    "ga_",
    "dynamic_link_",
    "notification_",
)
RESERVED_NAMES = (
    "ad_activeview",
    "ad_click",
    "ad_exposure",
    "ad_query",
    "ad_reward",
    "adunit_exposure",
    "app_background",
    "app_clear_data",
    "app_exception",
    "app_remove",
    "app_store_refund",
    "app_store_subscription_cancel",
    "app_store_subscription_convert",
    "app_store_subscription_renew",
    "app_update",
    "app_upgrade",
    "error",
    "first_open",
    "first_visit",
    "os_update",
    "session_start",
    "session_start_with_rollout",
    "user_engagement",
)

# Events
EVENT_PULSE_INSTALL = "pulse_install"
EVENT_PULSE_SESSION_START = "pulse_session_start"
EVENT_PULSE_FOREGROUND = "pulse_foreground"
EVENT_PULSE_BACKGROUND = "pulse_background"
EVENT_PULSE_HEARTBEAT = "pulse_heartbeat"
EVENT_PULSE_APP_UPDATE = "pulse_app_update"
EVENT_PULSE_ERROR = "pulse_error"
EVENT_PULSE_EXIT = "pulse_exit"
EVENT_PULSE_CONSENT = "pulse_consent"
EVENT_PULSE_PERF = "pulse_perf"
EVENT_PULSE_LOAD = "pulse_load"
EVENT_LEVEL_START = "level_start"
EVENT_LEVEL_COMPLETE = "level_complete"
EVENT_LEVEL_FAIL = "level_fail"
EVENT_LEVEL_QUIT = "level_quit"
EVENT_TUTORIAL_BEGIN = "tutorial_begin"
EVENT_TUTORIAL_STEP = "tutorial_step"
EVENT_TUTORIAL_COMPLETE = "tutorial_complete"
EVENT_SETTING_CHANGE = "setting_change"
EVENT_EARN_VIRTUAL_CURRENCY = "earn_virtual_currency"
EVENT_SPEND_VIRTUAL_CURRENCY = "spend_virtual_currency"
EVENT_PURCHASE = "purchase"
EVENT_AD_IMPRESSION = "ad_impression"
EVENT_AD_EVENT = "ad_event"
EVENT_SCREEN_VIEW = "screen_view"
AUTOMATIC_EVENTS = (
    "pulse_install",
    "pulse_session_start",
    "pulse_foreground",
    "pulse_background",
    "pulse_heartbeat",
    "pulse_app_update",
    "pulse_error",
    "pulse_exit",
    "pulse_consent",
    "pulse_perf",
    "pulse_load",
)
GAME_EVENTS = (
    "level_start",
    "level_complete",
    "level_fail",
    "level_quit",
    "tutorial_begin",
    "tutorial_step",
    "tutorial_complete",
    "setting_change",
    "earn_virtual_currency",
    "spend_virtual_currency",
    "purchase",
    "ad_impression",
    "ad_event",
)
APP_EVENTS = ("screen_view",)

# Event parameters
PARAM_INSTALL_STORE = "install_store"
PARAM_INSTALLER_RAW = "installer_raw"
PARAM_RESTORED = "restored"
PARAM_COLD = "cold"
PARAM_SINCE_LAST_S = "since_last_s"
PARAM_FG_MS = "fg_ms"
PARAM_FROM_VERSION = "from_version"
PARAM_TYPE = "type"
PARAM_MSG = "msg"
PARAM_FRAME = "frame"
PARAM_FP = "fp"
PARAM_FATAL = "fatal"
PARAM_REASON = "reason"
PARAM_PREV_SID = "prev_sid"
PARAM_PREV_VERSION = "prev_version"
PARAM_RSS_KB = "rss_kb"
PARAM_STATE = "state"
PARAM_PULSE_V = "pulse_v"
PARAM_TIER = "tier"
PARAM_SCENE = "scene"
PARAM_WINDOW_S = "window_s"
PARAM_FRAMES = "frames"
PARAM_FT_SRC = "ft_src"
PARAM_FT_P50_MS = "ft_p50_ms"
PARAM_FT_P95_MS = "ft_p95_ms"
PARAM_FT_MAX_MS = "ft_max_ms"
PARAM_SLOW_FRAMES = "slow_frames"
PARAM_JANKY_FRAMES = "janky_frames"
PARAM_CPU_MAIN_P95_MS = "cpu_main_p95_ms"
PARAM_GPU_P95_MS = "gpu_p95_ms"
PARAM_MEM_SRC = "mem_src"
PARAM_MEM_ALLOC_MB = "mem_alloc_mb"
PARAM_MEM_RESERVED_MB = "mem_reserved_mb"
PARAM_GC_RESERVED_MB = "gc_reserved_mb"
PARAM_SYS_USED_MB = "sys_used_mb"
PARAM_SYS_MEM_MB = "sys_mem_mb"
PARAM_KIND = "kind"
PARAM_DURATION_MS = "duration_ms"
PARAM_LEVEL = "level"
PARAM_LEVEL_NAME = "level_name"
PARAM_WORLD = "world"
PARAM_ATTEMPT = "attempt"
PARAM_MODE = "mode"
PARAM_SCORE = "score"
PARAM_STARS = "stars"
PARAM_MOVES_LEFT = "moves_left"
PARAM_PROGRESS = "progress"
PARAM_POS_X = "pos_x"
PARAM_POS_Y = "pos_y"
PARAM_POS_SPACE = "pos_space"
PARAM_TUTORIAL = "tutorial"
PARAM_STEP = "step"
PARAM_STEP_NAME = "step_name"
PARAM_SETTING = "setting"
PARAM_FROM = "from"
PARAM_TO = "to"
PARAM_VIRTUAL_CURRENCY_NAME = "virtual_currency_name"
PARAM_VALUE = "value"
PARAM_ITEM_NAME = "item_name"
PARAM_TRANSACTION_ID = "transaction_id"
PARAM_PRODUCT_ID = "product_id"
PARAM_PRICE_MICROS = "price_micros"
PARAM_CURRENCY = "currency"
PARAM_IS_RESTORE = "is_restore"
PARAM_IS_SANDBOX = "is_sandbox"
PARAM_AD_PLATFORM = "ad_platform"
PARAM_AD_FORMAT = "ad_format"
PARAM_AD_SOURCE = "ad_source"
PARAM_AD_UNIT_NAME = "ad_unit_name"
PARAM_PLACEMENT = "placement"
PARAM_PRECISION = "precision"
PARAM_ACTION = "action"
PARAM_ERROR_CODE = "error_code"
PARAM_SCREEN_NAME = "screen_name"
PARAM_SCREEN_CLASS = "screen_class"

# Context (ctx) and event fields
CTX_APP_ID = "app_id"
CTX_PLATFORM = "platform"
CTX_APP_VERSION = "app_version"
CTX_INSTALL_ID = "install_id"
CTX_BUILD = "build"
CTX_BUILD_ENV = "build_env"
CTX_INSTALL_STORE = "install_store"
CTX_INSTALLER_RAW = "installer_raw"
CTX_APP_NAME = "app_name"
CTX_SDK = "sdk"
CTX_ENGINE = "engine"
CTX_OS = "os"
CTX_DEVICE_MODEL = "device_model"
CTX_DEVICE_TIER = "device_tier"
CTX_RAM_MB = "ram_mb"
CTX_LOCALE = "locale"
CTX_TZ_OFFSET_MIN = "tz_offset_min"
CTX_CONSENT = "consent"
CTX_CHILD = "child"
FIELD_ID = "id"
FIELD_SEQ = "seq"
FIELD_NAME = "name"
FIELD_TS = "ts"
FIELD_SID = "sid"
FIELD_SNUM = "snum"
FIELD_FG_MS = "fg_ms"
FIELD_UID = "uid"
FIELD_P = "p"

# Enum values
PLATFORM_ANDROID = "android"
PLATFORM_IOS = "ios"
PLATFORM_EDITOR = "editor"
PLATFORM_WINDOWS = "windows"
PLATFORM_MACOS = "macos"
PLATFORM_LINUX = "linux"
PLATFORM_WEBGL = "webgl"
ENUM_PLATFORM = (
    "android",
    "ios",
    "editor",
    "windows",
    "macos",
    "linux",
    "webgl",
)
BUILD_ENV_RELEASE = "release"
BUILD_ENV_DEBUG = "debug"
BUILD_ENV_EDITOR = "editor"
ENUM_BUILD_ENV = (
    "release",
    "debug",
    "editor",
)
INSTALL_STORE_GOOGLE_PLAY = "google_play"
INSTALL_STORE_APP_STORE = "app_store"
INSTALL_STORE_TESTFLIGHT = "testflight"
INSTALL_STORE_AMAZON = "amazon"
INSTALL_STORE_SAMSUNG = "samsung"
INSTALL_STORE_HUAWEI = "huawei"
INSTALL_STORE_XIAOMI = "xiaomi"
INSTALL_STORE_SIDELOAD = "sideload"
INSTALL_STORE_SIMULATOR = "simulator"
INSTALL_STORE_EDITOR = "editor"
INSTALL_STORE_UNKNOWN = "unknown"
ENUM_INSTALL_STORE = (
    "google_play",
    "app_store",
    "testflight",
    "amazon",
    "samsung",
    "huawei",
    "xiaomi",
    "sideload",
    "simulator",
    "editor",
    "unknown",
)
CONSENT_GRANTED = "granted"
CONSENT_DENIED = "denied"
CONSENT_NOT_REQUIRED = "not_required"
CONSENT_UNKNOWN = "unknown"
ENUM_CONSENT = (
    "granted",
    "denied",
    "not_required",
    "unknown",
)
DEVICE_TIER_LOW = "low"
DEVICE_TIER_MID = "mid"
DEVICE_TIER_HIGH = "high"
DEVICE_TIER_UNKNOWN = "unknown"
ENUM_DEVICE_TIER = (
    "low",
    "mid",
    "high",
    "unknown",
)
EXIT_REASON_CRASH = "crash"
EXIT_REASON_CRASH_NATIVE = "crash_native"
EXIT_REASON_ANR = "anr"
EXIT_REASON_LOW_MEMORY = "low_memory"
EXIT_REASON_USER = "user"
EXIT_REASON_UNCLEAN = "unclean"
EXIT_REASON_UNKNOWN = "unknown"
ENUM_EXIT_REASON = (
    "crash",
    "crash_native",
    "anr",
    "low_memory",
    "user",
    "unclean",
    "unknown",
)
QUIT_REASON_MENU = "menu"
QUIT_REASON_RESTART = "restart"
QUIT_REASON_BACKGROUND_TIMEOUT = "background_timeout"
ENUM_QUIT_REASON = (
    "menu",
    "restart",
    "background_timeout",
)
SETTING_MUSIC = "music"
SETTING_SFX = "sfx"
SETTING_HAPTICS = "haptics"
SETTING_NOTIFICATIONS = "notifications"
SETTING_LANGUAGE = "language"
SETTING_QUALITY = "quality"
ENUM_SETTING = (
    "music",
    "sfx",
    "haptics",
    "notifications",
    "language",
    "quality",
)
AD_ACTION_REQUEST = "request"
AD_ACTION_FILL = "fill"
AD_ACTION_SHOW = "show"
AD_ACTION_CLICK = "click"
AD_ACTION_REWARD = "reward"
AD_ACTION_FAIL = "fail"
ENUM_AD_ACTION = (
    "request",
    "fill",
    "show",
    "click",
    "reward",
    "fail",
)
POS_SPACE_NORM = "norm"
POS_SPACE_WORLD = "world"
ENUM_POS_SPACE = (
    "norm",
    "world",
)
REGISTRY_STATUS_NEW = "new"
REGISTRY_STATUS_TESTING = "testing"
REGISTRY_STATUS_LIVE = "live"
ENUM_REGISTRY_STATUS = (
    "new",
    "testing",
    "live",
)
PRODUCTION_STORES = (
    "google_play",
    "app_store",
    "amazon",
    "samsung",
    "huawei",
    "xiaomi",
)
