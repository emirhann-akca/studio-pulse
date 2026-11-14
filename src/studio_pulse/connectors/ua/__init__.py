"""Ad-network spend (UA) connectors: Meta, Google Ads, TikTok, AppLovin, Unity Ads and CSV exports.

All of them write the `raw_ua_spend` contract (see common.py). Meta, Google Ads and
TikTok describe their endpoint as a dlt `rest_api` resource (rest.py); AppLovin (key in
the query string) and Unity Ads (CSV answers) use plain requests (report_client.py).
"""

from __future__ import annotations

DLT_SOURCES = ("meta_ads", "google_ads", "tiktok_ads")
DIRECT_SOURCES = ("applovin_ads", "unity_ads")
API_SOURCES = (*DLT_SOURCES, *DIRECT_SOURCES)
SOURCE_TYPES = (*API_SOURCES, "ua_csv")
