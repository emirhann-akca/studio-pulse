#!/usr/bin/env python3
"""Sets up an app's ad mediation (app + ad units) and writes the keys into the game's config.

Standard library only. Driven by a per-app manifest (JSON, kept in the game's repository):

    {
      "name": "My Game",
      "platforms": {
        "ios": {"bundle_id": "com.example.game", "app_store_id": "1234567890"},
        "android": {"package_name": "com.example.game"}
      },
      "ad_formats": ["rewarded", "interstitial"],
      "rewarded_item": {"name": "Coins", "amount": 1},
      "levelplay_taxonomy": "puzzle",
      "coppa": false,
      "networks": {
        "unity_ads": {
          "organization_core_id": "1234567890",
          "ios": {"game_id": "1000001", "placements": {"rewarded": "Rewarded_iOS", "interstitial": "Interstitial_iOS"}},
          "android": {"game_id": "1000002", "placements": {"rewarded": "Rewarded_Android", "interstitial": "Interstitial_Android"}}
        }
      },
      "privacy_policy_url": "https://example.com/my-game/privacy",
      "developer_website": "https://example.com",
      "unity_config": "Unity/MyGame/Assets/Resources/studio_app.json"
    }

    python tools/ads_onboard.py --manifest studio-app.json             # list, write what exists
    python tools/ads_onboard.py --manifest studio-app.json --create    # also create what is missing
    python tools/ads_onboard.py --manifest studio-app.json --provider max

Providers and the secrets they read from the environment (never printed):

  levelplay (default)  LEVELPLAY_SECRET_KEY, LEVELPLAY_REFRESH_TOKEN (LevelPlay platform: My Account)
  max                  APPLOVIN_MANAGEMENT_KEY (AppLovin dashboard: Account > General > Keys)

Mediated networks (LevelPlay): the ids in "networks" (Unity Ads game ids and placements) are
not app config, they belong in the LevelPlay platform (Mediation > Setup > SDK Networks). The tool
reads the app's network instances (Instances API v4, read only) and prints what is missing as a
checklist with the exact values; it does not write instances (the API's per-network field mapping
for Unity Ads is not documented).

Re-running changes nothing that is already right: apps and ad units are matched by bundle id,
platform and format and reused; the config file is rewritten only when a value differs. Without
--create nothing is created on the ad platform. --dry-run writes nothing anywhere.

APIs (read 15 Nov 2026):
  LevelPlay  https://docs.unity.com/en-us/grow/levelplay/platform/api/authentication
             GET  https://platform.ironsrc.com/partners/publisher/auth (secretkey, refreshToken
                  headers) -> bearer token, valid 24 h
             GET/POST https://platform.ironsrc.com/partners/publisher/applications/v6
             GET/POST/PUT https://platform.ironsrc.com/levelPlay/adUnits/v1/{appKey}
             GET  https://platform.ironsrc.com/levelPlay/network/instances/v4/{appKey}
  MAX        https://support.axon.ai/en/max/advanced-features/ad-unit-management-api
             GET  https://o.applovin.com/mediation/v1/ad_units, POST .../ad_unit (Api-Key header)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

PLATFORM_KEYS = {"ios": "bundle_id", "android": "package_name"}
FORMATS = ("rewarded", "interstitial", "banner")

# (method, url, headers, body) -> parsed answer (JSON, or the raw text when it is not JSON)
Transport = Callable[[str, str, dict[str, str], Any], Any]


class OnboardError(Exception):
    """A problem the user can fix; printed without a traceback."""


def mask(text: str, secrets: list[str]) -> str:
    """Removes every secret (and its URL-encoded form) from anything that could be printed."""
    for secret in secrets:
        if secret:
            for form in {secret, urllib.parse.quote(secret, safe="")}:
                text = text.replace(form, "***")
    return text


def http_transport(secrets: list[str], timeout: float = 30.0) -> Transport:
    def call(method: str, url: str, headers: dict[str, str], body: Any) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "studio-pulse-ads-onboard")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        path = urllib.parse.urlsplit(url).path
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https hosts)
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            hint = " (check the API credentials)" if e.code in (401, 403) else ""
            raise OnboardError(
                mask(f"{method} {path}: HTTP {e.code}{hint} {detail}", secrets)
            ) from None
        except urllib.error.URLError as e:
            raise OnboardError(mask(f"{method} {path}: {e.reason}", secrets)) from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.strip()

    return call


# ---------------------------------------------------------------------------- manifest


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise OnboardError(f"manifest {path}: {e}") from None
    if not isinstance(m, dict) or not str(m.get("name", "")).strip():
        raise OnboardError("manifest: 'name' is required")
    plats = m.get("platforms") or {}
    if not plats or any(p not in PLATFORM_KEYS for p in plats):
        raise OnboardError("manifest: 'platforms' must contain ios and/or android")
    for p, info in plats.items():
        if not str((info or {}).get(PLATFORM_KEYS[p], "")).strip():
            raise OnboardError(f"manifest: platforms.{p}.{PLATFORM_KEYS[p]} is required")
    formats = m.get("ad_formats") or ["rewarded", "interstitial"]
    bad = [f for f in formats if f not in FORMATS]
    if bad:
        raise OnboardError(f"manifest: unknown ad_formats {bad} (use {list(FORMATS)})")
    m["ad_formats"] = list(formats)
    return m


def bundle_ids(manifest: dict[str, Any]) -> dict[str, str]:
    """platform -> bundle id / package name."""
    return {p: str(info[PLATFORM_KEYS[p]]).strip() for p, info in manifest["platforms"].items()}


def store_url(manifest: dict[str, Any], platform: str) -> str | None:
    info = manifest["platforms"][platform]
    if info.get("store_url"):
        return str(info["store_url"]).strip()
    if platform == "ios" and str(info.get("app_store_id", "")).strip():
        return f"https://apps.apple.com/app/id{str(info['app_store_id']).strip()}"
    if platform == "android":
        return f"https://play.google.com/store/apps/details?id={info['package_name'].strip()}"
    return None


def unit_name(manifest: dict[str, Any], platform: str, fmt: str) -> str:
    plat = "iOS" if platform == "ios" else "Android"
    return f"{manifest['name'].strip()} {plat} {fmt.capitalize()}"


# ---------------------------------------------------------------------------- LevelPlay

LP_BASE = "https://platform.ironsrc.com"


class LevelPlay:
    """LevelPlay platform APIs: bearer token, applications v6, ad units v1."""

    config_section = "levelplay"

    def __init__(self, call: Transport, secret_key: str, refresh_token: str, base: str = LP_BASE):
        self.call = call
        self.base = base.rstrip("/")
        token = call(
            "GET",
            self.base + "/partners/publisher/auth",
            {"secretkey": secret_key, "refreshToken": refresh_token},
            None,
        )
        if not isinstance(token, str) or not token.strip('"'):
            raise OnboardError("LevelPlay auth: no bearer token in the answer")
        self.auth = {"Authorization": "Bearer " + token.strip('"')}

    def apps(self) -> list[dict[str, Any]]:
        apps = self.call("GET", self.base + "/partners/publisher/applications/v6", self.auth, None)
        if not isinstance(apps, list):
            raise OnboardError("LevelPlay applications: unexpected answer")
        return apps

    def find_app(self, platform: str, bundle: str) -> dict[str, Any] | None:
        want = "ios" if platform == "ios" else "android"
        found = [
            a
            for a in self.apps()
            if str(a.get("platform", "")).lower() == want and a.get("bundleId") == bundle
        ]
        found.sort(key=lambda a: (str(a.get("appStatus", "")).lower() != "active", a.get("appKey")))
        return found[0] if found else None

    def create_app(self, manifest: dict[str, Any], platform: str) -> None:
        url = store_url(manifest, platform)
        taxonomy = str(manifest.get("levelplay_taxonomy") or "").strip()
        if not url or not taxonomy:
            raise OnboardError(
                f"{platform}: creating a live LevelPlay app needs the store URL and levelplay_taxonomy"
            )
        body = {
            "storeUrl": url,
            "taxonomy": taxonomy,
            "appName": manifest["name"].strip(),
            "coppa": 1 if manifest.get("coppa") else 0,
        }
        self.call("POST", self.base + "/partners/publisher/applications/v6", self.auth, body)

    def units(self, app_key: str) -> list[dict[str, Any]]:
        url = f"{self.base}/levelPlay/adUnits/v1/{urllib.parse.quote(app_key, safe='')}"
        units = self.call("GET", url, self.auth, None) or []
        if not isinstance(units, list):
            raise OnboardError("LevelPlay ad units: unexpected answer")
        return units

    def create_units(self, manifest: dict[str, Any], app_key: str, platform: str, fmts: list[str]):
        reward = manifest.get("rewarded_item") or {"name": "Reward", "amount": 1}
        body = []
        for fmt in fmts:
            u: dict[str, Any] = {
                "mediationAdUnitName": unit_name(manifest, platform, fmt),
                "adFormat": fmt,
            }
            if fmt == "rewarded":
                u["reward"] = {
                    "rewardItemName": str(reward.get("name", "Reward"))[:32],
                    "rewardAmount": reward.get("amount", 1),
                }
            body.append(u)
        url = f"{self.base}/levelPlay/adUnits/v1/{urllib.parse.quote(app_key, safe='')}"
        self.call("POST", url, self.auth, body)

    def instances(self, app_key: str) -> list[dict[str, Any]]:
        url = f"{self.base}/levelPlay/network/instances/v4/{urllib.parse.quote(app_key, safe='')}"
        found = self.call("GET", url, self.auth, None) or []
        if not isinstance(found, list):
            raise OnboardError("LevelPlay instances: unexpected answer")
        return found

    def unpause(self, app_key: str, unit_ids: list[str]) -> None:
        url = f"{self.base}/levelPlay/adUnits/v1/{urllib.parse.quote(app_key, safe='')}"
        body = [{"mediationAdunitId": i, "isPaused": False} for i in unit_ids]
        self.call("PUT", url, self.auth, body)


def levelplay_pick(units: list[dict[str, Any]], manifest, platform: str, fmt: str):
    matches = [u for u in units if u.get("adFormat") == fmt]
    want = unit_name(manifest, platform, fmt)
    matches.sort(
        key=lambda u: (
            bool(u.get("isPaused")),
            u.get("mediationAdUnitName") != want,
            str(u.get("mediationAdUnitId")),
        )
    )
    return matches[0] if matches else None


def run_levelplay(lp: LevelPlay, manifest, create: bool, out) -> tuple[dict, list[str]]:
    """-> (config values to write {"iosAppKey": .., ("ios","rewarded"): id ...}, problems)."""
    values: dict[Any, str] = {}
    problems: list[str] = []
    for platform, bundle in bundle_ids(manifest).items():
        app = lp.find_app(platform, bundle)
        if app is None and create:
            lp.create_app(manifest, platform)
            app = lp.find_app(platform, bundle)
            if app is not None:
                out(f"  created LevelPlay app {platform}: {app.get('appKey')}")
        if app is None:
            problems.append(f"{platform}: no LevelPlay app for {bundle}")
            out(f"  {platform:<8} app MISSING ({bundle})")
            continue
        key = str(app["appKey"])
        values[f"{platform}AppKey"] = key
        out(f"  {platform:<8} app {key} ({app.get('appName', '')})")
        units = lp.units(key)
        missing = [
            f
            for f in manifest["ad_formats"]
            if levelplay_pick(units, manifest, platform, f) is None
        ]
        if missing and create:
            lp.create_units(manifest, key, platform, missing)
            units = lp.units(key)
            for f in missing:
                u = levelplay_pick(units, manifest, platform, f)
                if u is not None:
                    out(f"  created {platform} {f}: {u.get('mediationAdUnitId')}")
        paused = []
        for fmt in manifest["ad_formats"]:
            u = levelplay_pick(units, manifest, platform, fmt)
            if u is None:
                problems.append(f"{platform} {fmt}: no ad unit")
                out(f"  {platform:<8} {fmt:<13} MISSING")
                continue
            values[(platform, fmt)] = str(u["mediationAdUnitId"])
            state = " (paused)" if u.get("isPaused") else ""
            out(
                f"  {platform:<8} {fmt:<13} {u['mediationAdUnitId']} ({u.get('mediationAdUnitName', '')}){state}"
            )
            if u.get("isPaused"):
                paused.append(str(u["mediationAdUnitId"]))
        if paused and create:
            lp.unpause(key, paused)
            out(f"  {platform}: unpaused {', '.join(paused)}")
        elif paused:
            problems.append(f"{platform}: paused ad units {paused} (--create unpauses them)")
        problems += unity_ads_check(lp.instances(key), manifest, platform, out)
    return values, problems


def unity_ads_check(instances, manifest, platform: str, out) -> list[str]:
    """Unity Ads as a LevelPlay network: which ad formats have a Unity Ads instance (read only)."""
    net = ((manifest.get("networks") or {}).get("unity_ads") or {}).get(platform)
    if not net:
        return []
    org = ((manifest.get("networks") or {}).get("unity_ads") or {}).get("organization_core_id", "")
    problems = []
    for fmt in manifest["ad_formats"]:
        have = [
            i
            for i in instances
            if str(i.get("networkName", "")).lower() == "unityads"
            and str(i.get("adFormat", "")).lower() == fmt
        ]
        placement = (net.get("placements") or {}).get(fmt, "")
        if have:
            live = any(i.get("isLive") for i in have)
            kinds = ", ".join(
                sorted({"bidder" if i.get("isBidder") else "waterfall" for i in have})
            )
            out(f"  {platform:<8} {fmt:<13} Unity Ads {kinds}{'' if live else ' (inactive)'}")
            if not live:
                problems.append(f"{platform} {fmt}: Unity Ads instance inactive")
            continue
        problems.append(f"{platform} {fmt}: no Unity Ads instance")
        out(
            f"  {platform:<8} {fmt:<13} Unity Ads MISSING -> LevelPlay platform: Mediation > Setup >"
        )
        out(
            "           SDK Networks > Unity Ads (API Key + Organization core ID " + str(org) + "),"
        )
        out(
            f"           app row > Unity Ads > Game ID {net.get('game_id', '?')}, Placement ID {placement or '?'}"
        )
        out("           (or the auto-setup tool: Add bidder)")
    return problems


# ---------------------------------------------------------------------------- MAX

MAX_BASE = "https://o.applovin.com/mediation/v1"
MAX_FORMATS = {"rewarded": "REWARD", "interstitial": "INTER", "banner": "BANNER"}
PAGE = 100


class Max:
    config_section = "max"

    def __init__(self, call: Transport, key: str, base: str = MAX_BASE):
        self.call = call
        self.base = base.rstrip("/")
        self.auth = {"Api-Key": key}

    def units(self) -> list[dict[str, Any]]:
        units: list[dict[str, Any]] = []
        offset = 0
        while True:
            q = urllib.parse.urlencode({"limit": PAGE, "offset": offset})
            page = self.call("GET", f"{self.base}/ad_units?{q}", self.auth, None) or []
            if not isinstance(page, list):
                raise OnboardError("GET /ad_units: unexpected answer")
            units.extend(page)
            if len(page) < PAGE:
                return units
            offset += PAGE

    def create(self, manifest, platform: str, bundle: str, fmt: str) -> dict[str, Any]:
        body = {
            "name": unit_name(manifest, platform, fmt),
            "platform": platform,
            "package_name": bundle,
            "ad_format": MAX_FORMATS[fmt],
        }
        made = self.call("POST", f"{self.base}/ad_unit", self.auth, body)
        if not isinstance(made, dict) or not made.get("id"):
            raise OnboardError(f"POST /ad_unit {platform} {fmt}: no id in the answer")
        return made


def max_pick(units, manifest, platform: str, bundle: str, fmt: str):
    matches = [
        u
        for u in units
        if u.get("platform") == platform
        and u.get("package_name") == bundle
        and u.get("ad_format") == MAX_FORMATS[fmt]
        and not u.get("disabled")
    ]
    want = unit_name(manifest, platform, fmt)
    matches.sort(key=lambda u: (u.get("name") != want, str(u.get("id"))))
    return matches[0] if matches else None


def run_max(mx: Max, manifest, create: bool, out) -> tuple[dict, list[str]]:
    values: dict[Any, str] = {}
    problems: list[str] = []
    units = mx.units()
    for platform, bundle in bundle_ids(manifest).items():
        for fmt in manifest["ad_formats"]:
            u = max_pick(units, manifest, platform, bundle, fmt)
            if u is None and create:
                u = mx.create(manifest, platform, bundle, fmt)
                out(f"  created {platform} {fmt}: {u['id']}")
            if u is None:
                problems.append(f"{platform} {fmt}: no ad unit")
                out(f"  {platform:<8} {fmt:<13} MISSING")
                continue
            values[(platform, fmt)] = str(u["id"])
            out(f"  {platform:<8} {fmt:<13} {u['id']} ({u.get('name', '')})")
    return values, problems


# ---------------------------------------------------------------------------- config, app-ads.txt


def write_config(path: Path, section: str, values: dict[Any, str]) -> list[str]:
    """Sets <section>.<platform>.<format> and <section>.<key> values; returns what changed."""
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise OnboardError(f"{path}: not valid JSON ({e})") from None
    sec = data.setdefault(section, {})
    changed = []
    for k, v in sorted(values.items(), key=lambda kv: str(kv[0])):
        if isinstance(k, tuple):
            platform, fmt = k
            target, field, label = sec.setdefault(platform, {}), fmt, f"{section}.{platform}.{fmt}"
        else:
            target, field, label = sec, k, f"{section}.{k}"
        if target.get(field) != v:
            target[field] = v
            changed.append(label)
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed


def app_ads_txt(manifest: dict[str, Any], provider: str, account_id: str | None) -> list[str]:
    site = str(manifest.get("developer_website") or "").strip().rstrip("/")
    privacy = str(manifest.get("privacy_policy_url") or "").strip()
    host = urllib.parse.urlsplit(site).netloc if site else "<developer website>"
    lines = [
        "app-ads.txt",
        "  Format (IAB): <ad system domain>, <publisher account id>, DIRECT[, <certification id>]",
    ]
    if provider == "max":
        lines += [
            f"  applovin.com, {account_id or '<id from Account > General > App-ads.txt Info>'}, DIRECT",
            "  The AppLovin id and the line of every network MAX mediates are on the AppLovin",
            "  dashboard page Account > General > App-ads.txt Info (it is not the SDK key).",
        ]
    else:
        lines += [
            "  Copy the lines for LevelPlay (ironSource Ads) and every network it mediates from the",
            "  LevelPlay platform; this tool does not invent them (no API for them was found).",
        ]
    lines += [
        f"  Host it at https://{host}/app-ads.txt as text/plain (UTF-8), at the root of the domain:",
        "  - Google Play: the developer website in the store listing (Store presence > Store",
        "    listing > Developer contact) must be that domain;",
        "  - App Store: the same domain as the app's Privacy Policy URL"
        + (f" ({urllib.parse.urlsplit(privacy).netloc})." if privacy else "."),
    ]
    return lines


# ---------------------------------------------------------------------------- main


ENV = {
    "levelplay": ("LEVELPLAY_SECRET_KEY", "LEVELPLAY_REFRESH_TOKEN"),
    "max": ("APPLOVIN_MANAGEMENT_KEY",),
}


def run(
    manifest_path: Path,
    provider: str,
    create: bool,
    dry_run: bool,
    account_id: str | None,
    client: LevelPlay | Max | None,
    out: Callable[[str], None] = print,
) -> int:
    manifest = load_manifest(manifest_path)
    out(
        f"{manifest['name']} ({provider}): "
        + ", ".join(f"{p} {b}" for p, b in bundle_ids(manifest).items())
    )
    if client is None:
        out(f"{' and '.join(ENV[provider])} not set: nothing listed or created.")
        for line in app_ads_txt(manifest, provider, account_id):
            out(line)
        return 2
    do_create = create and not dry_run
    if isinstance(client, LevelPlay):
        values, problems = run_levelplay(client, manifest, do_create, out)
    else:
        values, problems = run_max(client, manifest, do_create, out)
    if problems and not create:
        out("  missing or paused: run again with --create")
    cfg_rel = str(manifest.get("unity_config") or "").strip()
    if cfg_rel:
        cfg = (manifest_path.parent / cfg_rel).resolve()
        if dry_run:
            out(f"  --dry-run: {cfg} not written")
        else:
            changed = write_config(cfg, client.config_section, values)
            out(
                f"  {cfg}: "
                + (", ".join(changed) + " updated" if changed else "already up to date")
            )
    for line in app_ads_txt(manifest, provider, account_id):
        out(line)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", required=True, type=Path, help="the app's studio-app.json")
    ap.add_argument("--provider", choices=sorted(ENV), default="levelplay")
    ap.add_argument("--create", action="store_true", help="create what is missing (apps, ad units)")
    ap.add_argument("--dry-run", action="store_true", help="list only; create and write nothing")
    ap.add_argument(
        "--applovin-account-id", help="MAX: the id from Account > General > App-ads.txt Info"
    )
    a = ap.parse_args(argv)
    secrets = [os.environ.get(name, "").strip() for name in ENV[a.provider]]
    client: LevelPlay | Max | None = None
    try:
        if all(secrets):
            call = http_transport(secrets)
            client = (
                LevelPlay(call, *secrets) if a.provider == "levelplay" else Max(call, secrets[0])
            )
        return run(a.manifest, a.provider, a.create, a.dry_run, a.applovin_account_id, client)
    except OnboardError as e:
        print("error: " + mask(str(e), secrets), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
