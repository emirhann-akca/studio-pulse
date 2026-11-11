"""Synthetic store reviews and Android vitals for the demo.

Written in the stores' own formats and read by the real connectors:
  reviews/<app>/play/reviews_<package>_YYYYMM.csv   Play Console monthly export (UTF-16)
  reviews/<app>/play_api/                           Play Developer API reviews.list,
                                                    the last 7 days (recorded responses)
  reviews/<app>/app_store/                          App Store Connect customerReviews
  vitals/<app>/                                     Play Developer Reporting API

About 3 reviews per 1,000 daily users; ratings average about 4.1 (5 stars 55%,
1 star 10%). Quartz Merge's bad 1.8.0 release gets crash and lag complaints and
a higher crash and ANR rate in vitals. Texts are short invented sentences, in
English and, for Turkish players, Turkish. Own random stream per app.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import random
from pathlib import Path
from typing import Any

TEXTS = {
    5: [
        "Love this game, so relaxing",
        "Best puzzle game I have played",
        "Great fun, addictive levels",
        "Awesome, I play every day",
        "Çok güzel bir oyun, harika",
    ],
    4: [
        "Fun but a few too many ads",
        "Great game, levels get hard later",
        "Nice, would like more levels",
        "Good app, the subscription price is a bit high",
        "Güzel ama reklam biraz fazla",
    ],
    3: [
        "Too many ads after each level",
        "OK but stuck on level 45 for days",
        "Slow loading on my phone",
        "Price of boosters is too expensive",
        "Fena değil ama çok zor",
    ],
    2: [
        "Ads every 30 seconds, uninstalling soon",
        "Lost my progress after the update",
        "Lag on my old phone",
        "Pay to win, impossible without boosters",
        "Reklam çok fazla, sıkıldım",
    ],
    1: [
        "Crashes on start, cannot play",
        "Freezes and closes itself",
        "Black screen after update",
        "Charged twice for the subscription, want a refund",
        "Oyun sürekli çöküyor",
    ],
}
INCIDENT_TEXTS = [
    "Crashes since the last update",
    "Keeps freezing on level start, so laggy now",
    "New version is broken, crash every time",
    "Güncellemeden sonra donuyor ve çöküyor",
]
NORMAL = [(5, 0.55), (4, 0.20), (3, 0.09), (2, 0.06), (1, 0.10)]
BAD = [(5, 0.20), (4, 0.10), (3, 0.08), (2, 0.12), (1, 0.50)]
TERRITORIES = {
    "US": "USA",
    "GB": "GBR",
    "DE": "DEU",
    "FR": "FRA",
    "JP": "JPN",
    "BR": "BRA",
    "MX": "MEX",
    "TR": "TUR",
    "IN": "IND",
    "ID": "IDN",
}
DEVICES = ["SM-A055F", "Redmi 12C", "SM-A546B", "Pixel 6a", "Pixel 8 Pro", "OnePlus 12"]


def _poisson(lam: float, r: random.Random) -> int:
    import math

    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= r.random()
        if p <= L:
            return k
        k += 1


def _version(app: Any, di: int, days: int) -> str:
    rel = sorted(
        app.versions
        + ([(days - app.incident.days_before_end, app.incident.version)] if app.incident else [])
    )
    cur = rel[0][1]
    for d, v in rel:
        if d <= di:
            cur = v
    return cur


def generate(gen: Any, out: Path) -> dict[str, Any]:
    """Reviews and vitals for one demo app; returns the config sources."""
    app = gen.app
    r = random.Random(app.seed * 13 + 7)
    sources: dict[str, Any] = {}
    play: list[dict[str, Any]] = []
    apple: list[dict[str, Any]] = []
    n = 0
    for di in range(gen.days):
        day = gen.start + dt.timedelta(days=di)
        version = _version(app, di, gen.days)
        bad = bool(app.incident and version == app.incident.version)
        for platform in app.platforms:
            dau = gen.dau_by_day.get((di, platform), 0)
            for _ in range(_poisson(dau * 0.003, r)):
                n += 1
                dist = BAD if bad and platform == "ANDROID" else NORMAL
                rating = r.choices([x for x, _ in dist], weights=[w for _, w in dist], k=1)[0]
                text = (
                    r.choice(INCIDENT_TEXTS)
                    if bad and rating <= 2 and platform == "ANDROID"
                    else r.choice(TEXTS[rating])
                )
                tr = any(ch in text for ch in "çğıöşüÇĞİÖŞÜ")
                country = "TR" if tr else r.choice(["US", "GB", "DE", "BR", "IN", "JP", "MX"])
                when = dt.datetime.combine(day, dt.time()) + dt.timedelta(
                    seconds=r.uniform(3600, 82000)
                )
                row = {
                    "n": n,
                    "when": when,
                    "rating": rating,
                    "text": text,
                    "version": version,
                    "lang": "tr" if tr else "en",
                    "country": country,
                    "reply": rating <= 2 and r.random() < 0.4,
                }
                (play if platform == "ANDROID" else apple).append(row)
    root = out / "reviews" / app.id
    if play:
        _write_play_csv(root / "play", app.package, play, r)
        _write_play_api(
            root / "play_api", app.package, play, gen.start + dt.timedelta(days=gen.days - 1)
        )
        sources["play_reviews"] = {
            "path": f"reviews/{app.id}/play",
            "package": app.package,
            "api": True,
            "replay": f"reviews/{app.id}/play_api",
        }
        _write_vitals(out / "vitals" / app.id, app, gen, r)
        sources["android_vitals"] = {"package": app.package, "replay": f"vitals/{app.id}"}
    if apple:
        _write_asc(root / "app_store", app.apple_id, apple)
        sources["app_store_reviews"] = {
            "apple_id": app.apple_id,
            "replay": f"reviews/{app.id}/app_store",
        }
    return sources


PLAY_HEAD = [
    "Package Name",
    "App Version Code",
    "App Version Name",
    "Reviewer Language",
    "Device",
    "Review Submit Date and Time",
    "Review Submit Millis Since Epoch",
    "Review Last Update Date and Time",
    "Review Last Update Millis Since Epoch",
    "Star Rating",
    "Review Title",
    "Review Text",
    "Developer Reply Date and Time",
    "Developer Reply Millis Since Epoch",
    "Developer Reply Text",
    "Review Link",
]


def _ms(t: dt.datetime) -> int:
    return int((t - dt.datetime(1970, 1, 1)).total_seconds() * 1000)


def _write_play_csv(d: Path, package: str, rows: list[dict[str, Any]], r: random.Random) -> None:
    d.mkdir(parents=True, exist_ok=True)
    months: dict[str, list[list[str]]] = {}
    for x in rows:
        t = x["when"]
        reply_t = t + dt.timedelta(hours=20)
        months.setdefault(t.strftime("%Y%m"), []).append(
            [
                package,
                x["version"].replace(".", "0"),
                x["version"],
                x["lang"],
                r.choice(DEVICES),
                t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                str(_ms(t)),
                t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                str(_ms(t)),
                str(x["rating"]),
                "",
                x["text"],
                reply_t.strftime("%Y-%m-%dT%H:%M:%SZ") if x["reply"] else "",
                str(_ms(reply_t)) if x["reply"] else "",
                "Thanks for the report, a fix is on the way." if x["reply"] else "",
                f"https://play.google.com/console/developers/demo/app/demo/user-feedback/review-details?reviewId=gp:demo-{x['n']:06d}",
            ]
        )
    for month, mrows in sorted(months.items()):
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(PLAY_HEAD)
        w.writerows(mrows)
        (d / f"reviews_{package}_{month}.csv").write_bytes(buf.getvalue().encode("utf-16"))


def _write_play_api(d: Path, package: str, rows: list[dict[str, Any]], last: dt.date) -> None:
    d.mkdir(parents=True, exist_ok=True)
    recent = [x for x in rows if x["when"].date() > last - dt.timedelta(days=7)]
    recent.sort(key=lambda x: x["when"], reverse=True)
    reviews = []
    for x in recent:
        comments: list[dict[str, Any]] = [
            {
                "userComment": {
                    "text": "\t" + x["text"],
                    "lastModified": {"seconds": str(_ms(x["when"]) // 1000), "nanos": 0},
                    "starRating": x["rating"],
                    "reviewerLanguage": x["lang"],
                    "device": "demo",
                    "appVersionName": x["version"],
                }
            }
        ]
        if x["reply"]:
            comments.append({"developerComment": {"text": "Thanks for the report."}})
        reviews.append(
            {
                "reviewId": f"gp:demo-{x['n']:06d}",
                "authorName": "A Google user",
                "comments": comments,
            }
        )
    pages = [reviews[i : i + 100] for i in range(0, len(reviews), 100)] or [[]]
    entries = []
    path = f"/androidpublisher/v3/applications/{package}/reviews"
    for i, page in enumerate(pages):
        body: dict[str, Any] = {"reviews": page}
        if i + 1 < len(pages):
            body["tokenPagination"] = {"nextPageToken": f"p{i + 2}"}
        (d / f"reviews_{i + 1}.json").write_text(json.dumps(body), encoding="utf-8")
        e: dict[str, Any] = {"method": "GET", "path": path, "file": f"reviews_{i + 1}.json"}
        if i:
            e["query"] = {"token": f"p{i + 1}"}
        entries.append(e)
    (d / "replay.json").write_text(json.dumps({"requests": entries}), encoding="utf-8")


def _write_asc(d: Path, apple_id: str, rows: list[dict[str, Any]]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda x: x["when"], reverse=True)
    pages = [rows[i : i + 200] for i in range(0, len(rows), 200)] or [[]]
    path = f"/v1/apps/{apple_id}/customerReviews"
    entries = []
    for i, page in enumerate(pages):
        body: dict[str, Any] = {
            "data": [
                {
                    "type": "customerReviews",
                    "id": f"00000000-demo-{x['n']:06d}",
                    "attributes": {
                        "rating": x["rating"],
                        "title": x["text"].split(",")[0],
                        "body": x["text"],
                        "reviewerNickname": "demo player",
                        "createdDate": x["when"].strftime("%Y-%m-%dT%H:%M:%S-00:00"),
                        "territory": TERRITORIES[x["country"]],
                    },
                }
                for x in page
            ],
            "links": {"self": f"https://api.appstoreconnect.apple.com{path}"},
        }
        if i + 1 < len(pages):
            body["links"]["next"] = f"https://api.appstoreconnect.apple.com{path}?cursor=c{i + 2}"
        (d / f"reviews_{i + 1}.json").write_text(json.dumps(body), encoding="utf-8")
        e: dict[str, Any] = {"method": "GET", "path": path, "file": f"reviews_{i + 1}.json"}
        if i:
            e["query"] = {"cursor": f"c{i + 1}"}
        entries.append(e)
    (d / "replay.json").write_text(json.dumps({"requests": entries}), encoding="utf-8")


def _write_vitals(d: Path, app: Any, gen: Any, r: random.Random) -> None:
    d.mkdir(parents=True, exist_ok=True)
    crashed: dict[int, set[str]] = {}
    for t, plat, fatal, uid in zip(
        gen.crash.get("event_timestamp", []),
        gen.crash.get("platform", []),
        gen.crash.get("is_fatal", []),
        gen.crash.get("installation_uuid", []),
        strict=False,
    ):
        if plat == "ANDROID" and fatal:
            crashed.setdefault((t.date() - gen.start).days, set()).add(uid)
    rows: dict[str, list[dict[str, Any]]] = {"anrRateMetricSet": [], "crashRateMetricSet": []}
    for di in range(gen.days):
        day = gen.start + dt.timedelta(days=di)
        dau = gen.dau_by_day.get((di, "ANDROID"), 0)
        if not dau:
            continue
        bad = bool(app.incident and _version(app, di, gen.days) == app.incident.version)
        crash = len(crashed.get(di, ())) / dau
        anr = max(0.0, r.gauss(0.0042 if bad else 0.0021, 0.0004))
        start = {
            "year": day.year,
            "month": day.month,
            "day": day.day,
            "timeZone": {"id": "America/Los_Angeles"},
        }

        def m(name: str, v: float) -> dict[str, Any]:
            return {"metric": name, "decimalValue": {"value": f"{v:.6f}"}}

        rows["anrRateMetricSet"].append(
            {
                "aggregationPeriod": "DAILY",
                "startTime": start,
                "metrics": [
                    m("anrRate", anr * 1.3),
                    m("userPerceivedAnrRate", anr),
                    m("distinctUsers", dau),
                ],
            }
        )
        rows["crashRateMetricSet"].append(
            {
                "aggregationPeriod": "DAILY",
                "startTime": start,
                "metrics": [
                    m("crashRate", crash),
                    m("userPerceivedCrashRate", crash * 0.85),
                    m("distinctUsers", dau),
                ],
            }
        )
    entries = []
    for metric_set, items in rows.items():
        name = f"{metric_set}.json"
        (d / name).write_text(json.dumps({"rows": items}), encoding="utf-8")
        entries.append(
            {
                "method": "POST",
                "path": f"/v1beta1/apps/{app.package}/{metric_set}:query",
                "file": name,
            }
        )
    (d / "replay.json").write_text(json.dumps({"requests": entries}), encoding="utf-8")
