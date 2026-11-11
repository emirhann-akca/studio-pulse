"""Ad-network spend (UA) connectors: Meta, Google Ads, TikTok and CSV exports.

All of them write the `raw_ua_spend` contract (see common.py); the API
connectors describe their endpoint as a dlt `rest_api` resource (rest.py).
"""

from __future__ import annotations

API_SOURCES = ("meta_ads", "google_ads", "tiktok_ads")
SOURCE_TYPES = (*API_SOURCES, "ua_csv")
