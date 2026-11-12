# Event contract v2

<!-- GENERATED from contract/pulse_v2.yaml by `python -m studio_pulse.contract.generate`. Do not edit. -->

One YAML file, [`contract/pulse_v2.yaml`](../contract/pulse_v2.yaml), defines every
event, parameter, enum value and limit. The generator writes the code every party
uses, and CI fails when a regenerated file differs from the committed one:

| Output | Used by |
|---|---|
| `src/studio_pulse/contract/pulse_v2.json` | collector validation (shipped in the wheel) |
| `src/studio_pulse/contract/generated.py` | Python code (`from studio_pulse.contract import generated as C`) |
| `unity/com.studio.pulse/Runtime/PulseContract.g.cs` | Unity SDK (`Studio.Pulse.PulseContract`) |
| `flutter/studio_pulse_perf/lib/src/contract.g.dart` | Flutter SDK (`PulseEvents`, `PulseParams` ...) |
| `contract/generated/pulse_v2.batch.schema.json` | JSON Schema of a batch (any language) |
| `contract/generated/bigquery/events.json` | BigQuery `pulse_raw.events` |
| `contract/generated/bigquery/quarantine.json` | BigQuery `pulse_raw.quarantine` |
| `contract/generated/bigquery/app_registry.json` | BigQuery `pulse_raw.app_registry` |
| `contract/generated/bigquery/deletion_requests.json` | BigQuery `pulse_raw.deletion_requests` |
| `contract/generated/PrivacyInfo.xcprivacy` | iOS plug-in privacy manifest (copy into the plug-in) |
| `docs/CONTRACT.md` | people (this page) |

Contract v1 (`pulse_perf` / `pulse_load` as JSON lines, [EVENTS.md](EVENTS.md)) stays
valid: the collector accepts v1 lines on `POST /v1/ndjson` and upgrades them.

## Rules

- **Names**: `^[a-z][a-z0-9_]{0,39}$` (GA4: 1-40 chars), at most 25 parameters per event,
  string values at most 100 characters unless a parameter says otherwise.
- **Reserved**: names Firebase drops are refused (`ad_activeview`, `ad_click`, `ad_exposure`, `ad_query`, `ad_reward`, `adunit_exposure`, `app_background`, `app_clear_data`, `app_exception`, `app_remove`, `app_store_refund`, `app_store_subscription_cancel`, `app_store_subscription_convert`, `app_store_subscription_renew`, `app_update`, `app_upgrade`, `error`, `first_open`, `first_visit`, `os_update`, `session_start`, `session_start_with_rollout`, `user_engagement`),
  as are the prefixes `firebase_`, `google_`, `ga_`, `dynamic_link_`, `notification_`.
- **`pulse_` prefix**: only the SDK's automatic events use it; an unknown `pulse_*` name is refused.
- **Custom events**: any other valid name, with scalar parameters (string, number, bool). Keep the set small and static.
- **Ids**: `id` is a UUIDv7 made on the device, `seq` a per-install counter kept on disk.
  The collector and the warehouse drop repeats of `id` and of (`install_id`, `seq`); gaps in `seq` measure loss.
- **Time**: `ts` and `sent_at` are device-clock milliseconds. The collector stores
  `ts = received_at - (sent_at - ts_client)` (Segment's skew correction) next to the raw value,
  quarantines events more than 60 min in the future after correction
  or before 1577836800000 ms (2020), and flags events older than 30 days as `late`.
- **Sizes**: at most 500 events and 1,048,576 bytes per batch (uncompressed and
  inflated), at most 8,192 bytes per event as compact JSON. Too large -> HTTP 413 (the SDK halves the batch);
  an oversized event is quarantined.
- **Privacy**: random `install_id` only; never IDFA, AAID, IDFV, Android ID, e-mail, name or IP.
  String values that look like an e-mail address are quarantined. `consent: denied` batches are
  not stored; `child: true` strips `uid` and free-text parameters. See [PRIVACY.md](PRIVACY.md).
- **SDK behaviour** (limits below): flush at 20 events / 30 s / background / quit,
  session timeout 30 min in the background, retry with backoff
  2-60 s plus jitter on network errors, 408, 429 and 5xx; drop on 400; disable until the next launch on 401/403.

## Batch envelope (`POST /v2/batch`)

Headers: `Content-Type: application/json`, optional `Content-Encoding: gzip`, `X-Pulse-Key: <public write key>`.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `v` | int | yes |  | Envelope version, always 2 |
| `schema` | string | yes | <= 16 chars | Contract version the SDK was built with |
| `sent_at` | int | yes |  | Device clock when the request was sent, ms since epoch |
| `ctx` | object | yes |  | Context shared by every event of the batch |
| `events` | array | yes |  | 1-500 events |

### `ctx`

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `app_id` | string | yes | <= 150 chars | Bundle id / package name |
| `platform` | string | yes | one of `platform` |  |
| `app_version` | string | yes | <= 50 chars |  |
| `install_id` | string | yes | UUID | Random UUIDv4 per install; never a device or ad id |
| `build` | string |  | <= 50 chars | Build number |
| `build_env` | string |  | one of `build_env` | release, debug or editor |
| `install_store` | string |  | one of `install_store` | Where the install came from (Android installer, iOS AppTransaction.environment) |
| `installer_raw` | string |  | <= 100 chars, may be null | Installer package as reported, for anything the enum does not cover |
| `app_name` | string |  | <= 100 chars | Display name; the registry uses it for new apps |
| `sdk` | string | yes | <= 50 chars | unity/2.0.0, flutter/2.0.0 ... |
| `engine` | string |  | <= 50 chars |  |
| `os` | string |  | <= 50 chars |  |
| `device_model` | string |  | <= 100 chars |  |
| `device_tier` | string |  | one of `device_tier` |  |
| `ram_mb` | int |  | >= 0 |  |
| `locale` | string |  | <= 35 chars |  |
| `tz_offset_min` | int |  | >= -900, <= 900 |  |
| `consent` | string |  | one of `consent` | denied: the collector stores nothing from this batch |
| `child` | bool |  |  | Child-directed mode: uid and free-text params are stripped |

### Event

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `id` | string | yes | UUID | UUIDv7 made on the device; dedupe key |
| `seq` | int | yes | >= 0 | Per-install counter persisted on disk; gaps = loss, (install_id, seq) = dedupe key |
| `name` | string | yes | <= 100 chars | Event name (rules above) |
| `ts` | int | yes |  | Device clock, ms since epoch |
| `sid` | string | yes | UUID | Session id |
| `snum` | int |  | >= 1 | Session number of this install, from 1 |
| `fg_ms` | int |  | >= 0 | Foreground time in this session so far, ms |
| `uid` | string |  | <= 100 chars, may be null | Optional game account id (never e-mail); null in child mode |
| `p` | object |  |  | Parameters (event catalog below) |

## Event catalog

### Automatic (the SDK sends these)

#### `pulse_install`

First launch of a new install_id (or an Android backup restore).

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `install_store` | string | yes | one of `install_store` |  |
| `installer_raw` | string | yes | <= 100 chars, may be null |  |
| `restored` | bool | yes |  | install_id came back from an Android backup |

#### `pulse_session_start`

Cold start, or foreground after more than session_timeout_ms in the background.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `cold` | bool | yes |  |  |
| `since_last_s` | int |  | >= 0 | Seconds since the previous session ended |

#### `pulse_foreground`

App came to the foreground.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `fg_ms` | int |  | >= 0 |  |

#### `pulse_background`

App went to the background; triggers a flush.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `fg_ms` | int |  | >= 0 |  |

#### `pulse_heartbeat`

Every 60 s in the foreground, only if nothing else was sent in that minute.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `fg_ms` | int |  | >= 0 |  |

#### `pulse_app_update`

app_version differs from the last launch.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `from_version` | string | yes | <= 50 chars |  |

#### `pulse_error`

Managed exception (Unity log hook, Flutter error hooks); at most errors_per_launch unique fingerprints per launch.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `type` | string | yes | <= 100 chars |  |
| `msg` | string |  | <= 256 chars, free text (dropped in child mode) | Scrubbed message (paths and numbers removed) |
| `frame` | string |  | <= 100 chars | Top stack frame |
| `fp` | string | yes | <= 20 chars | 64-bit fingerprint, hex |
| `fatal` | bool | yes |  |  |

#### `pulse_exit`

Sent at the next launch with the reason the previous process ended.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `reason` | string | yes | one of `exit_reason` |  |
| `prev_sid` | string |  | <= 36 chars |  |
| `prev_version` | string |  | <= 50 chars |  |
| `rss_kb` | int |  | >= 0 |  |

#### `pulse_consent`

Consent state changed.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `state` | string | yes | one of `consent` |  |

#### `pulse_perf`

One frame-time window (contract v1, unchanged; see docs/EVENTS.md).

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `pulse_v` | int |  |  |  |
| `tier` | string |  | one of `device_tier` |  |
| `scene` | string |  | <= 100 chars |  |
| `window_s` | double |  | >= 0 |  |
| `frames` | int |  | >= 0 |  |
| `ft_src` | string |  | <= 20 chars |  |
| `ft_p50_ms` | double |  | >= 0 |  |
| `ft_p95_ms` | double |  | >= 0 |  |
| `ft_max_ms` | double |  | >= 0 |  |
| `slow_frames` | int |  | >= 0 |  |
| `janky_frames` | int |  | >= 0 |  |
| `cpu_main_p95_ms` | double |  | >= 0 |  |
| `gpu_p95_ms` | double |  | >= 0 |  |
| `mem_src` | string |  | <= 20 chars |  |
| `mem_alloc_mb` | int |  | >= 0 |  |
| `mem_reserved_mb` | int |  | >= 0 |  |
| `gc_reserved_mb` | int |  | >= 0 |  |
| `sys_used_mb` | int |  | >= 0 |  |
| `sys_mem_mb` | int |  | >= 0 |  |

#### `pulse_load`

A cold start or scene load (contract v1, unchanged).

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `pulse_v` | int |  |  |  |
| `tier` | string |  | one of `device_tier` |  |
| `kind` | string | yes | <= 20 chars |  |
| `scene` | string |  | <= 100 chars |  |
| `duration_ms` | double | yes | >= 0 |  |

### Game

#### `level_start`

A level attempt begins.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `level` | int | yes | >= 0 |  |
| `level_name` | string |  | <= 100 chars, free text (dropped in child mode) |  |
| `world` | string |  | <= 100 chars |  |
| `attempt` | int |  | >= 1 |  |
| `mode` | string |  | <= 100 chars |  |

#### `level_complete`

The level was won.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `level` | int | yes | >= 0 |  |
| `attempt` | int | yes | >= 1 |  |
| `duration_ms` | int | yes | >= 0 |  |
| `score` | int |  |  |  |
| `stars` | int |  | >= 0, <= 5 |  |
| `moves_left` | int |  |  |  |

#### `level_fail`

The level was lost; pos_x/pos_y feed the death heatmap.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `level` | int | yes | >= 0 |  |
| `attempt` | int | yes | >= 1 |  |
| `duration_ms` | int | yes | >= 0 |  |
| `reason` | string | yes | <= 40 chars | Short static code: out_of_moves, out_of_time, hit_spike ... |
| `progress` | double |  | >= 0, <= 1 | Share of the level done, 0-1 |
| `pos_x` | double |  |  |  |
| `pos_y` | double |  |  |  |
| `pos_space` | string |  | one of `pos_space` | norm (0-1 of the level bounds, default) or world units |
| `score` | int |  |  |  |

#### `level_quit`

The player left a level without winning or losing.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `level` | int | yes | >= 0 |  |
| `attempt` | int | yes | >= 1 |  |
| `duration_ms` | int | yes | >= 0 |  |
| `reason` | string | yes | one of `quit_reason` |  |
| `progress` | double |  | >= 0, <= 1 |  |
| `pos_x` | double |  |  |  |
| `pos_y` | double |  |  |  |
| `pos_space` | string |  | one of `pos_space` |  |

#### `tutorial_begin`

Tutorial started (GA4 recommended name).

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `tutorial` | string |  | <= 100 chars | Which tutorial, when there are several |

#### `tutorial_step`

A tutorial step was shown.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `step` | int | yes | >= 0 |  |
| `step_name` | string |  | <= 100 chars, free text (dropped in child mode) |  |
| `tutorial` | string |  | <= 100 chars |  |

#### `tutorial_complete`

Tutorial finished (GA4 recommended name).

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `tutorial` | string |  | <= 100 chars |  |

#### `setting_change`

A setting changed; one event with setting=..., not music_off.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `setting` | string | yes | one of `setting` |  |
| `from` | string |  | <= 20 chars | Previous value: on/off, a language code, a quality level |
| `to` | string | yes | <= 20 chars |  |

#### `earn_virtual_currency`

Soft currency earned (GA4 recommended name).

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `virtual_currency_name` | string | yes | <= 40 chars |  |
| `value` | double | yes |  |  |
| `item_name` | string |  | <= 100 chars |  |
| `level` | int |  | >= 0 |  |

#### `spend_virtual_currency`

Soft currency spent (GA4 recommended name).

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `virtual_currency_name` | string | yes | <= 40 chars |  |
| `value` | double | yes |  |  |
| `item_name` | string |  | <= 100 chars |  |
| `level` | int |  | >= 0 |  |

#### `purchase`

Real-money purchase. Not forwarded to Firebase (GA4 logs in_app_purchase itself). Not forwarded to Firebase.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `transaction_id` | string | yes | <= 100 chars |  |
| `product_id` | string | yes | <= 100 chars |  |
| `price_micros` | int | yes | >= 0 | Price paid in micros of currency |
| `currency` | string | yes | <= 3 chars | ISO 4217 |
| `is_restore` | bool |  |  |  |
| `is_sandbox` | bool |  |  |  |

#### `ad_impression`

Impression-level ad revenue (Firebase ad revenue parameters).

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `ad_platform` | string | yes | <= 40 chars |  |
| `ad_format` | string | yes | <= 40 chars |  |
| `value` | double | yes | >= 0 | Revenue in currency, dot decimal |
| `currency` | string | yes | <= 3 chars |  |
| `ad_source` | string |  | <= 100 chars |  |
| `ad_unit_name` | string |  | <= 100 chars |  |
| `placement` | string |  | <= 100 chars |  |
| `precision` | string |  | <= 20 chars |  |

#### `ad_event`

Ad lifecycle step.

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `action` | string | yes | one of `ad_action` |  |
| `ad_format` | string | yes | <= 40 chars |  |
| `placement` | string |  | <= 100 chars |  |
| `error_code` | string |  | <= 40 chars |  |

### App

#### `screen_view`

A screen was shown (GA4 parameter names).

| Key | Type | Required | Rule | Meaning |
|---|---|---|---|---|
| `screen_name` | string | yes | <= 100 chars |  |
| `screen_class` | string |  | <= 100 chars |  |

## Enums

| Enum | Values |
|---|---|
| `platform` | `android`, `ios`, `editor`, `windows`, `macos`, `linux`, `webgl` |
| `build_env` | `release`, `debug`, `editor` |
| `install_store` | `google_play`, `app_store`, `testflight`, `amazon`, `samsung`, `huawei`, `xiaomi`, `sideload`, `simulator`, `editor`, `unknown` |
| `consent` | `granted`, `denied`, `not_required`, `unknown` |
| `device_tier` | `low`, `mid`, `high`, `unknown` |
| `exit_reason` | `crash`, `crash_native`, `anr`, `low_memory`, `user`, `unclean`, `unknown` |
| `quit_reason` | `menu`, `restart`, `background_timeout` |
| `setting` | `music`, `sfx`, `haptics`, `notifications`, `language`, `quality` |
| `ad_action` | `request`, `fill`, `show`, `click`, `reward`, `fail` |
| `pos_space` | `norm`, `world` |
| `registry_status` | `new`, `testing`, `live` |

Production stores (count towards a game being live): `google_play`, `app_store`, `amazon`, `samsung`, `huawei`, `xiaomi`.

## Limits

| Limit | Value |
|---|---|
| `batch_max_events` | 500 |
| `batch_max_bytes` | 1048576 |
| `event_max_bytes` | 8192 |
| `name_max_len` | 40 |
| `max_params` | 25 |
| `string_max_len` | 100 |
| `future_tolerance_ms` | 3600000 |
| `late_after_ms` | 2592000000 |
| `min_ts_ms` | 1577836800000 |
| `session_timeout_ms` | 1800000 |
| `session_max_ms` | 86400000 |
| `errors_per_launch` | 10 |
| `flush_events` | 20 |
| `flush_interval_ms` | 30000 |
| `heartbeat_ms` | 60000 |
| `queue_max_events` | 10000 |
| `queue_max_bytes` | 5242880 |
| `retry_min_ms` | 2000 |
| `retry_max_ms` | 60000 |
| `raw_retention_days` | 396 |
| `deletion_max_days` | 90 |

## Privacy mapping

What the SDK collects, for Apple's privacy manifest and App Store labels and Google Play's
Data safety form ([PRIVACY.md](PRIVACY.md); legal review needed before a store submission).

| Data | Apple (`NSPrivacyCollectedDataType`) | Google Play Data safety | Comes from |
|---|---|---|---|
| Product interaction | `ProductInteraction` | App activity: App interactions | sessions, level_*, tutorial_*, setting_change, screen_view, earn/spend_virtual_currency, ad_event, custom events |
| Crash data | `CrashData` | App info and performance: Crash logs | pulse_error, pulse_exit |
| Performance data | `PerformanceData` | App info and performance: Diagnostics | pulse_perf, pulse_load |
| Other diagnostic data | `OtherDiagnosticData` | App info and performance: Diagnostics | ctx: os, device_model, device_tier, ram_mb, sdk, engine |
| Device ID | `DeviceID` | Device or other IDs | ctx.install_id (random UUID per install; no IDFA, AAID, IDFV or Android ID) |
| User ID | `UserID` | Personal info: User IDs | event uid, only when the game sets an account id (never in child mode) |
| Purchase history | `PurchaseHistory` | Financial info: Purchase history | purchase |
| Coarse location | `CoarseLocation` | Location: Approximate location | country derived from the request IP by the collector; the IP is never stored |

## Stored tables

### `events`

One row per accepted event. Partition by DATE(received_at), cluster by app_id, name.

| Column | Type | Mode | Meaning |
|---|---|---|---|
| `event_id` | STRING | REQUIRED |  |
| `seq` | INT64 | NULLABLE |  |
| `name` | STRING | REQUIRED |  |
| `ts` | TIMESTAMP | REQUIRED | Server-corrected time: received_at - (sent_at - ts_client) |
| `ts_client` | TIMESTAMP | NULLABLE | Device clock as sent |
| `sent_at` | TIMESTAMP | NULLABLE |  |
| `received_at` | TIMESTAMP | REQUIRED |  |
| `skew_ms` | INT64 | NULLABLE | received_at - sent_at (device clock error plus transit) |
| `late` | BOOL | NULLABLE | ts older than late_after_ms when received |
| `sid` | STRING | NULLABLE |  |
| `snum` | INT64 | NULLABLE |  |
| `fg_ms` | INT64 | NULLABLE |  |
| `uid` | STRING | NULLABLE |  |
| `params` | STRING | NULLABLE | Event parameters as a JSON object (text, so every sink can write it) |
| `app_id` | STRING | REQUIRED |  |
| `platform` | STRING | REQUIRED |  |
| `app_version` | STRING | NULLABLE |  |
| `build` | STRING | NULLABLE |  |
| `build_env` | STRING | NULLABLE |  |
| `install_id` | STRING | REQUIRED |  |
| `install_store` | STRING | NULLABLE |  |
| `installer_raw` | STRING | NULLABLE |  |
| `sdk` | STRING | NULLABLE |  |
| `engine` | STRING | NULLABLE |  |
| `os` | STRING | NULLABLE |  |
| `device_model` | STRING | NULLABLE |  |
| `device_tier` | STRING | NULLABLE |  |
| `ram_mb` | INT64 | NULLABLE |  |
| `locale` | STRING | NULLABLE |  |
| `tz_offset_min` | INT64 | NULLABLE |  |
| `consent` | STRING | NULLABLE |  |
| `child` | BOOL | NULLABLE |  |
| `country` | STRING | NULLABLE | ISO 3166-1 alpha-2 from the request IP (IP discarded) or unknown |
| `key_id` | STRING | NULLABLE | Which write key (hash prefix), for rotation |
| `schema` | STRING | NULLABLE | Contract version the SDK sent (1 for upgraded v1 lines) |

### `quarantine`

Events and batches that failed validation, with the reason; counted by the data-quality checks.

| Column | Type | Mode | Meaning |
|---|---|---|---|
| `received_at` | TIMESTAMP | REQUIRED |  |
| `reason` | STRING | REQUIRED |  |
| `app_id` | STRING | NULLABLE |  |
| `platform` | STRING | NULLABLE |  |
| `install_id` | STRING | NULLABLE |  |
| `event_id` | STRING | NULLABLE |  |
| `name` | STRING | NULLABLE |  |
| `raw` | STRING | NULLABLE | The event (or batch head) as received, truncated to 8 KB |
| `key_id` | STRING | NULLABLE |  |

### `app_registry`

One row per (app_id, platform), created by the first event; status new -> testing -> live.

| Column | Type | Mode | Meaning |
|---|---|---|---|
| `app_id` | STRING | REQUIRED |  |
| `platform` | STRING | REQUIRED |  |
| `display_name` | STRING | NULLABLE |  |
| `genre` | STRING | NULLABLE |  |
| `status` | STRING | REQUIRED |  |
| `first_seen` | TIMESTAMP | REQUIRED |  |
| `last_seen` | TIMESTAMP | NULLABLE |  |
| `first_version` | STRING | NULLABLE |  |
| `last_version` | STRING | NULLABLE |  |
| `status_changed_at` | TIMESTAMP | NULLABLE |  |
| `store_listed` | BOOL | NULLABLE |  |
| `store_version` | STRING | NULLABLE |  |
| `store_url` | STRING | NULLABLE |  |
| `store_checked_at` | TIMESTAMP | NULLABLE |  |

### `deletion_requests`

Deletion requests by install_id (POST /v2/delete); executed by the next run, within deletion_max_days.

| Column | Type | Mode | Meaning |
|---|---|---|---|
| `install_id` | STRING | REQUIRED |  |
| `app_id` | STRING | NULLABLE |  |
| `requested_at` | TIMESTAMP | REQUIRED |  |
| `reason` | STRING | NULLABLE |  |
| `status` | STRING | NULLABLE |  |
| `done_at` | TIMESTAMP | NULLABLE |  |
