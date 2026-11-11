"""Synthetic UA spend for the demo, written in each network's own format.

A share of every app's new users is bought: each bought user is assigned a
network, a campaign (by the user's country) and a creative. The networks'
reports are then derived from those users, so the demo's ROAS is the real
revenue of real (synthetic) cohorts against spend:

  Meta, Google Ads, TikTok   recorded API responses (a replay directory per
                             network), read by the real connectors
  AppLovin, Unity Ads        CSV exports, read by the ua_csv importer

Google Ads users carry their campaign in GA4's first-touch traffic_source, as
App campaigns linked to Firebase do; the other networks' users look organic /
direct to GA4 (no MMP), so their ROAS is modelled from network installs.
Everything uses its own random stream (seeded per app), so the existing demo
numbers do not change. Ranges: CPI 0.1-1.6 USD by country for games (about
3x for the fitness app), IPM 4-15, CTR 0.9-3%.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..connectors.ua.creative import cf1_name

FIRST_SPEND_DAY = 8  # spend starts on day 8 of the 75 demo days
EUR_PER_USD = 1 / 1.08  # Mossy Match's Meta account is billed in EUR (demo FX)

GEO_CPI = {  # relative CPI by country (US = 1)
    "US": 1.0,
    "GB": 0.85,
    "DE": 0.8,
    "FR": 0.7,
    "JP": 0.9,
    "BR": 0.36,
    "MX": 0.4,
    "TR": 0.3,
    "IN": 0.16,
    "ID": 0.18,
}
TIER1 = {"US", "GB", "DE", "FR", "JP"}


@dataclass
class Creative:
    name: str
    ipm: float
    weight: float
    first_day: int = 0  # day index it starts delivering


@dataclass
class Campaign:
    network: str
    id: str
    name: str
    geos: set[str] | None  # None: worldwide
    creatives: list[Creative]
    platforms: tuple[str, ...] = ("ANDROID", "IOS")


@dataclass
class Plan:
    paid_share: float  # share of new users bought
    networks: dict[str, float]  # network -> share of bought users
    base_cpi: dict[str, float]
    ctr: dict[str, float]
    campaigns: list[Campaign]
    over_report: dict[str, tuple[float, float]] = field(default_factory=dict)


def _cf(
    spec: str,
    concept: str,
    hook: str = "main",
    segment: str = "main",
    end: str = "main",
    fmt: str = "9x16",
    lang: str = "en",
) -> str:
    return cf1_name(spec, concept, hook, segment, end, fmt, lang)


PLANS: dict[str, Plan] = {
    "mossy_match": Plan(
        paid_share=0.34,
        networks={"google_ads": 0.4, "meta": 0.38, "applovin": 0.22},
        base_cpi={"google_ads": 0.9, "meta": 1.0, "applovin": 0.8},
        ctr={"google_ads": 0.015, "meta": 0.011, "applovin": 0.025},
        over_report={"google_ads": (0.9, 1.05), "meta": (0.98, 1.15), "applovin": (0.95, 1.1)},
        campaigns=[
            Campaign(
                "google_ads",
                "21001",
                "UAC_Android_Tier1",
                TIER1,
                [Creative(_cf("mossy-ads", "moss-sweep"), 7.0, 1.0)],
                ("ANDROID",),
            ),
            Campaign(
                "google_ads",
                "21002",
                "UAC_Android_Global",
                None,
                [Creative(_cf("mossy-ads", "moss-sweep", "fast"), 9.0, 1.0)],
                ("ANDROID",),
            ),
            Campaign(
                "meta",
                "23851000001",
                "Meta_AEO_Tier1",
                TIER1,
                [
                    Creative(_cf("mossy-ads", "satisfying-clean"), 6.0, 0.5),
                    Creative(
                        _cf(
                            "mossy-ads", "satisfying-clean", "blower", "lvl21", "fill", "9x16", "tr"
                        ),
                        9.0,
                        0.35,
                    ),
                    Creative(_cf("mossy-ads", "fail-bait"), 4.0, 0.15),
                    Creative(
                        _cf("mossy-ads", "fail-bait", "fast", "lvl3", "tap", "4x5"), 5.0, 0.05, 70
                    ),
                ],
            ),
            Campaign(
                "meta",
                "23851000002",
                "Meta_AEO_RoW",
                None,
                [
                    Creative(_cf("mossy-ads", "satisfying-clean"), 8.0, 0.6),
                    Creative(_cf("mossy-ads", "fail-bait"), 5.0, 0.4),
                ],
            ),
            Campaign(
                "applovin",
                "al_7001",
                "AL_Puzzle_ROAS",
                None,
                [
                    Creative(_cf("mossy-ads", "playable-sort", fmt="9x16"), 15.0, 0.6),
                    Creative(_cf("mossy-ads", "satisfying-clean"), 10.0, 0.4),
                ],
            ),
        ],
    ),
    "quartz_merge": Plan(
        paid_share=0.3,
        networks={"tiktok": 0.65, "unity_ads": 0.35},
        base_cpi={"tiktok": 0.7, "unity_ads": 0.65},
        ctr={"tiktok": 0.009, "unity_ads": 0.03},
        over_report={"tiktok": (1.0, 1.2), "unity_ads": (0.95, 1.1)},
        campaigns=[
            Campaign(
                "tiktok",
                "1790000000000001",
                "TT_Merge_AEO",
                None,
                [
                    Creative(_cf("quartz-ads", "coin-rain"), 8.0, 0.45),
                    Creative(
                        _cf("quartz-ads", "coin-rain", "fast", "lvl3", "tap", "4x5", "pt-br"),
                        12.0,
                        0.35,
                    ),
                    Creative(_cf("quartz-ads", "merge-asmr"), 5.0, 0.2),
                ],
                ("ANDROID",),
            ),
            Campaign(
                "unity_ads",
                "u_301",
                "UA_Merge_Global",
                None,
                [Creative("Merge pack 01 (legacy name)", 11.0, 1.0)],
                ("ANDROID",),
            ),
        ],
    ),
    "pacer_pal": Plan(
        paid_share=0.32,
        networks={"meta": 0.7, "google_ads": 0.3},
        base_cpi={"meta": 3.0, "google_ads": 2.7},
        ctr={"meta": 0.009, "google_ads": 0.012},
        over_report={"meta": (0.98, 1.15), "google_ads": (0.9, 1.05)},
        campaigns=[
            Campaign(
                "meta",
                "23852000001",
                "Meta_Trials_Tier1",
                TIER1,
                [
                    Creative(_cf("pacer-ads", "morning-run"), 3.0, 0.6),
                    Creative(_cf("pacer-ads", "streak-badge"), 4.0, 0.4),
                ],
            ),
            Campaign(
                "meta",
                "23852000002",
                "Meta_Trials_RoW",
                None,
                [
                    Creative(_cf("pacer-ads", "morning-run"), 5.0, 1.0),
                ],
            ),
            Campaign(
                "google_ads",
                "21101",
                "UAC_Fitness_Global",
                None,
                [Creative(_cf("pacer-ads", "streak-badge"), 4.5, 1.0)],
                ("ANDROID",),
            ),
        ],
    ),
}
ACCOUNTS = {
    ("mossy_match", "meta"): "1000000001",
    ("pacer_pal", "meta"): "1000000002",
    ("mossy_match", "google_ads"): "1000000011",
    ("pacer_pal", "google_ads"): "1000000012",
    ("quartz_merge", "tiktok"): "7000000000000000003",
}


class UASim:
    """Assigns bought users and writes the networks' reports."""

    def __init__(self, app_id: str, seed: int, start: dt.date) -> None:
        self.plan = PLANS.get(app_id)
        self.app_id = app_id
        self.rng = random.Random(seed * 7919 + 17)
        self.start = start
        # (day, network, campaign id, creative index, country, platform) -> users
        self.bought: dict[tuple[int, str, str, int, str, str], int] = defaultdict(int)

    def traffic(self, day_idx: int, country: str, platform: str) -> tuple[str, str, str]:
        """Decide whether a new user was bought; return GA4's first-touch traffic_source."""
        organic = (
            ("google-play", "organic", "(organic)")
            if platform == "ANDROID"
            else ("(direct)", "(none)", "(direct)")
        )
        p = self.plan
        if p is None or day_idx < FIRST_SPEND_DAY:
            return organic
        r = self.rng
        if r.random() >= p.paid_share:
            return organic
        nets = list(p.networks)
        network = r.choices(nets, weights=[p.networks[n] for n in nets], k=1)[0]
        options = [
            c
            for c in p.campaigns
            if c.network == network
            and platform in c.platforms
            and (c.geos is None or country in c.geos)
        ]
        # Tier-1 users go to the tier-1 campaign when there is one.
        tier = [c for c in options if c.geos is not None]
        if tier and country in TIER1:
            options = tier
        elif country not in TIER1:
            options = [c for c in options if c.geos is None]
        if not options:
            return organic
        camp = options[0]
        live = [i for i, cr in enumerate(camp.creatives) if day_idx >= cr.first_day]
        ci = r.choices(live, weights=[camp.creatives[i].weight for i in live], k=1)[0]
        self.bought[(day_idx, network, camp.id, ci, country, platform)] += 1
        if network == "google_ads":
            return ("google", "cpc", camp.name)
        return ("(direct)", "(none)", "(direct)")

    # -- reports -----------------------------------------------------------------
    def _rows(self) -> list[dict[str, Any]]:
        """One report row per day x campaign x creative x country (USD)."""
        p, r = self.plan, self.rng
        assert p is not None
        camps = {(c.network, c.id): c for c in p.campaigns}
        out = []
        for key in sorted(self.bought):
            day_idx, network, cid, ci, country, platform = key
            users = self.bought[key]
            camp = camps[(network, cid)]
            cr = camp.creatives[ci]
            lo, hi = p.over_report.get(network, (1.0, 1.0))
            installs = users * r.uniform(lo, hi)
            if network != "google_ads":
                installs = float(round(installs))
            cpi = p.base_cpi[network] * GEO_CPI.get(country, 0.3) * r.uniform(0.85, 1.15)
            spend = round(max(installs, 0.5) * cpi, 2)
            imps = int(round(max(installs, 0.5) * 1000 / cr.ipm * r.uniform(0.9, 1.1)))
            clicks = int(round(imps * p.ctr[network] * r.uniform(0.85, 1.15)))
            out.append(
                {
                    "date": self.start + dt.timedelta(days=day_idx),
                    "network": network,
                    "campaign_id": cid,
                    "campaign_name": camp.name,
                    "creative": cr.name,
                    "ad_id": f"{cid}{ci:02d}",
                    "country": country,
                    "platform": platform,
                    "spend": spend,
                    "impressions": imps,
                    "clicks": clicks,
                    "installs": installs,
                }
            )
        return out

    def write(self, out: Path) -> dict[str, Any]:
        """Write every network's report; return the config sources for them."""
        if self.plan is None or not self.bought:
            return {}
        import importlib.util

        api_ok = importlib.util.find_spec("dlt") is not None
        rows = self._rows()
        by_net: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_net[row["network"]].append(row)
        root = out / "ua" / self.app_id
        sources: dict[str, Any] = {}
        for network, nrows in sorted(by_net.items()):
            d = root / network
            d.mkdir(parents=True, exist_ok=True)
            if network == "meta":
                acc = ACCOUNTS[(self.app_id, "meta")]
                eur = self.app_id == "mossy_match"
                _write_meta(d, acc, nrows, eur)
                if api_ok:
                    sources["meta_ads"] = {
                        "account_id": acc,
                        "replay": f"ua/{self.app_id}/meta",
                        "lookback_days": 75,
                    }
            elif network == "google_ads":
                cust = ACCOUNTS[(self.app_id, "google_ads")]
                _write_google(d, cust, nrows)
                if api_ok:
                    sources["google_ads"] = {
                        "customer_id": cust,
                        "replay": f"ua/{self.app_id}/google_ads",
                        "lookback_days": 75,
                        "platform": "ANDROID",
                    }
                    if self.app_id == "pacer_pal":
                        # Subscription revenue reaches the warehouse through RevenueCat,
                        # not GA4 purchase events, so GA4 first-touch revenue would be 0.
                        sources["google_ads"]["attribution"] = "network_installs"
            elif network == "tiktok":
                adv = ACCOUNTS[(self.app_id, "tiktok")]
                _write_tiktok(d, adv, nrows)
                if api_ok:
                    sources["tiktok_ads"] = {
                        "advertiser_id": adv,
                        "replay": f"ua/{self.app_id}/tiktok",
                        "lookback_days": 75,
                        "platform": "ANDROID",
                    }
            else:
                _write_csv(d, network, nrows)
                key = "ua_csv" if "ua_csv" not in sources else f"ua_csv@{network}"
                sources[key] = {"network": network, "path": f"ua/{self.app_id}/{network}"}
        return sources


def _pages(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)] or [[]]


def _dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")


def _write_meta(d: Path, account: str, rows: list[dict[str, Any]], eur: bool) -> None:
    data = []
    for x in rows:
        spend = round(x["spend"] * EUR_PER_USD, 2) if eur else x["spend"]
        data.append(
            {
                "account_id": account,
                "account_currency": "EUR" if eur else "USD",
                "campaign_id": x["campaign_id"],
                "campaign_name": x["campaign_name"],
                "adset_id": x["campaign_id"] + "1",
                "adset_name": "broad",
                "ad_id": x["ad_id"],
                "ad_name": x["creative"],
                "country": x["country"],
                "impressions": str(x["impressions"]),
                "clicks": str(x["clicks"]),
                "spend": f"{spend:.2f}",
                "actions": [
                    {"action_type": "mobile_app_install", "value": str(int(x["installs"]))}
                ],
                "date_start": x["date"].isoformat(),
                "date_stop": x["date"].isoformat(),
            }
        )
    path = f"/v27.0/act_{account}/insights"
    entries = []
    pages = _pages(data, 500)
    for i, page in enumerate(pages):
        body: dict[str, Any] = {"data": page, "paging": {"cursors": {"after": f"p{i + 2}"}}}
        if i + 1 < len(pages):
            body["paging"]["next"] = (
                f"https://graph.facebook.com{path}?access_token=demo-not-a-token&after=p{i + 2}"
            )
        name = f"insights_{i + 1}.json"
        _dump(d / name, body)
        entry: dict[str, Any] = {"method": "GET", "path": path, "file": name}
        if i:
            entry["query"] = {"after": f"p{i + 1}"}
        entries.append(entry)
    _dump(d / "replay.json", {"requests": entries})


def _write_google(d: Path, customer: str, rows: list[dict[str, Any]]) -> None:
    results = [
        {
            "customer": {"id": customer, "currencyCode": "USD"},
            "campaign": {"id": x["campaign_id"], "name": x["campaign_name"]},
            "adGroup": {"id": x["campaign_id"] + "1", "name": "Ad group 1"},
            "adGroupAd": {"ad": {"id": x["ad_id"], "name": x["creative"]}},
            "metrics": {
                "costMicros": str(int(round(x["spend"] * 1e6))),
                "impressions": str(x["impressions"]),
                "clicks": str(x["clicks"]),
                "biddableAppInstallConversions": round(x["installs"], 2),
            },
            "segments": {"date": x["date"].isoformat()},
        }
        for x in _merge_countries(rows)
    ]
    path = f"/v26/customers/{customer}/googleAds:search"
    entries = []
    pages = _pages(results, 1000)
    for i, page in enumerate(pages):
        body: dict[str, Any] = {"results": page, "fieldMask": "segments.date"}
        if i + 1 < len(pages):
            body["nextPageToken"] = f"t{i + 2}"
        name = f"search_{i + 1}.json"
        _dump(d / name, body)
        entry: dict[str, Any] = {"method": "POST", "path": path, "file": name}
        if i:
            entry["json"] = {"pageToken": f"t{i + 1}"}
        entries.append(entry)
    _dump(d / "replay.json", {"requests": entries})


def _merge_countries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Google Ads ad_group_ad rows have no country: sum countries per day and ad."""
    acc: dict[tuple, dict[str, Any]] = {}
    for x in rows:
        k = (x["date"], x["campaign_id"], x["ad_id"])
        if k not in acc:
            acc[k] = {**x, "spend": 0.0, "impressions": 0, "clicks": 0, "installs": 0.0}
        a = acc[k]
        a["spend"] = round(a["spend"] + x["spend"], 2)
        a["impressions"] += x["impressions"]
        a["clicks"] += x["clicks"]
        a["installs"] += x["installs"]
    return [acc[k] for k in sorted(acc)]


def _write_tiktok(d: Path, advertiser: str, rows: list[dict[str, Any]]) -> None:
    items = [
        {
            "dimensions": {
                "ad_id": x["ad_id"],
                "stat_time_day": f"{x['date']} 00:00:00",
                "country_code": x["country"],
            },
            "metrics": {
                "campaign_id": x["campaign_id"],
                "campaign_name": x["campaign_name"],
                "adgroup_id": x["campaign_id"] + "1",
                "adgroup_name": "broad",
                "ad_name": x["creative"],
                "currency": "USD",
                "spend": f"{x['spend']:.2f}",
                "impressions": str(x["impressions"]),
                "clicks": str(x["clicks"]),
                "app_install": str(int(x["installs"])),
            },
        }
        for x in rows
    ]
    pages = _pages(items, 1000)
    entries = []
    for i, page in enumerate(pages):
        body = {
            "code": 0,
            "message": "OK",
            "request_id": f"demo-{i + 1}",
            "data": {
                "list": page,
                "page_info": {
                    "page": i + 1,
                    "page_size": 1000,
                    "total_number": len(items),
                    "total_page": len(pages),
                },
            },
        }
        name = f"report_{i + 1}.json"
        _dump(d / name, body)
        q = {"advertiser_id": advertiser}
        if i:
            q["page"] = str(i + 1)
        entries.append(
            {
                "method": "GET",
                "path": "/open_api/v1.3/report/integrated/get/",
                "query": q,
                "file": name,
            }
        )
    _dump(d / "replay.json", {"requests": entries})


def _write_csv(d: Path, network: str, rows: list[dict[str, Any]]) -> None:
    head = {
        "applovin": [
            "Day",
            "Campaign",
            "Campaign ID",
            "Creative Set",
            "Creative Set ID",
            "Country",
            "Platform",
            "Impressions",
            "Clicks",
            "Installs",
            "Cost",
        ],
        "unity_ads": [
            "Date",
            "Campaign name",
            "Campaign ID",
            "Creative pack name",
            "Creative pack ID",
            "Country",
            "Platform",
            "Starts",
            "Clicks",
            "Installs",
            "Spend",
        ],
    }[network]
    months: dict[str, list[list[str]]] = defaultdict(list)
    for x in rows:
        months[x["date"].strftime("%Y%m")].append(
            [
                x["date"].isoformat(),
                x["campaign_name"],
                x["campaign_id"],
                x["creative"],
                x["ad_id"],
                x["country"].lower(),
                x["platform"].lower(),
                str(x["impressions"]),
                str(x["clicks"]),
                str(int(x["installs"])),
                f"{x['spend']:.2f}",
            ]
        )
    for month, mrows in sorted(months.items()):
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(head)
        w.writerows(mrows)
        (d / f"{network}_{month}.csv").write_text(buf.getvalue(), encoding="utf-8")
