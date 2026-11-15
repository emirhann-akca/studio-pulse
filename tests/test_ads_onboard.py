"""tools/ads_onboard.py against fixtures shaped like the documented API answers.

LevelPlay (platform.ironsrc.com: auth, applications v6, ad units v1) and AppLovin MAX
(o.applovin.com Ad Unit Management API) are faked in memory: no account, no network.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FIX = ROOT / "tests/fixtures/ads_onboard"
SECRET, REFRESH, MGMT = "lp-secret-5f3a9c", "lp-refresh-77ab01", "max-management-key-0c1d2e"

spec = importlib.util.spec_from_file_location("ads_onboard", ROOT / "tools/ads_onboard.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeLevelPlay:
    """platform.ironsrc.com in memory; records every call."""

    def __init__(self):
        self.apps = fixture("levelplay_apps.json")
        self.units = {"1a2b3c4d": fixture("levelplay_units_1a2b3c4d.json")}
        self.calls = []
        self.next_id = 0

    def __call__(self, method, url, headers, body):
        path = url.split("platform.ironsrc.com", 1)[1].split("?")[0]
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/partners/publisher/auth":
            assert headers == {"secretkey": SECRET, "refreshToken": REFRESH}
            return "BEARER-TOKEN"
        assert headers == {"Authorization": "Bearer BEARER-TOKEN"}, path
        if path == "/partners/publisher/applications/v6":
            if method == "GET":
                return copy.deepcopy(self.apps)
            key = f"new{len(self.apps):05d}"
            platform = "iOS" if "apps.apple.com" in body["storeUrl"] else "Android"
            bundle = "com.example.puzzle"
            self.apps.append(
                {"appKey": key, "appName": body["appName"], "appStatus": "active",
                 "platform": platform, "bundleId": bundle}
            )  # fmt: skip
            self.units[key] = []
            return None
        if path.startswith("/levelPlay/adUnits/v1/"):
            key = path.rsplit("/", 1)[1]
            units = self.units.setdefault(key, [])
            if method == "GET":
                return copy.deepcopy(units)
            if method == "POST":
                for u in body:
                    self.next_id += 1
                    units.append(
                        {**u, "mediationAdUnitId": f"id{self.next_id:014d}", "isPaused": False}
                    )
                return None
            if method == "PUT":
                for change in body:
                    for u in units:
                        if u["mediationAdUnitId"] == change["mediationAdunitId"]:
                            u["isPaused"] = change["isPaused"]
                return None
        raise AssertionError(f"unexpected {method} {path}")

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class FakeMax:
    def __init__(self, page_size_units=0):
        self.units = fixture("max_ad_units.json")
        # pad the account so listing needs two pages
        for i in range(page_size_units):
            self.units.append(
                {"name": f"filler {i}", "platform": "ios", "package_name": f"com.filler.n{i}",
                 "ad_format": "BANNER", "id": f"f{i:015d}", "disabled": False}
            )  # fmt: skip
        self.calls = []

    def __call__(self, method, url, headers, body):
        assert headers == {"Api-Key": MGMT}
        path, _, query = url.split("o.applovin.com/mediation/v1", 1)[1].partition("?")
        self.calls.append((method, path, query, copy.deepcopy(body)))
        if method == "GET" and path == "/ad_units":
            q = dict(p.split("=") for p in query.split("&"))
            off, lim = int(q["offset"]), int(q["limit"])
            return copy.deepcopy([u for u in self.units if not u.get("disabled")][off : off + lim])
        if method == "POST" and path == "/ad_unit":
            made = {**body, "id": f"new{len(self.units):013d}", "disabled": False}
            self.units.append(made)
            return made
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def app_dir(tmp_path):
    (tmp_path / "studio-app.json").write_text((FIX / "studio-app.json").read_text(encoding="utf-8"))
    cfg = tmp_path / "Assets/Resources/studio_app.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {"pulse": {"collectorUrl": "", "writeKey": ""}, "levelplay": {"testSuite": False}}
        )
    )
    return tmp_path


def run(app_dir, client, provider="levelplay", create=False, dry_run=False):
    lines = []
    code = tool.run(
        app_dir / "studio-app.json", provider, create, dry_run, None, client, lines.append
    )
    return code, "\n".join(lines)


def config(app_dir):
    return json.loads((app_dir / "Assets/Resources/studio_app.json").read_text(encoding="utf-8"))


def test_levelplay_lists_then_creates_then_changes_nothing(app_dir):
    fake = FakeLevelPlay()
    lp = tool.LevelPlay(fake, SECRET, REFRESH)

    code, out = run(app_dir, lp)
    assert code == 1 and "run again with --create" in out
    assert fake.writes() == [], "listing creates nothing"
    cfg = config(app_dir)
    assert cfg["levelplay"]["iosAppKey"] == "1a2b3c4d", "the active app, not the archived one"
    assert cfg["levelplay"]["ios"] == {"rewarded": "rw00000000000001"}
    assert cfg["levelplay"]["testSuite"] is False and cfg["pulse"]["writeKey"] == "", (
        "other keys kept"
    )
    assert "android" in out and "app MISSING" in out

    code, out = run(app_dir, lp, create=True)
    assert code == 0, out
    posts = [c for c in fake.writes() if c[0] == "POST"]
    app_post = next(c for c in posts if c[1] == "/partners/publisher/applications/v6")
    assert app_post[2] == {
        "storeUrl": "https://play.google.com/store/apps/details?id=com.example.puzzle",
        "taxonomy": "puzzle", "appName": "Example Puzzle", "coppa": 0,
    }  # fmt: skip
    ios_units = next(c for c in posts if c[1] == "/levelPlay/adUnits/v1/1a2b3c4d")[2]
    assert ios_units == [
        {"mediationAdUnitName": "Example Puzzle iOS Interstitial", "adFormat": "interstitial"}
    ]
    android_units = [c for c in posts if c[1].startswith("/levelPlay/adUnits/v1/new")][0][2]
    rewarded = next(u for u in android_units if u["adFormat"] == "rewarded")
    assert rewarded["reward"] == {"rewardItemName": "Coins", "rewardAmount": 1}, (
        "rewarded needs its reward"
    )
    puts = [c for c in fake.writes() if c[0] == "PUT"]
    assert puts == [
        (
            "PUT",
            "/levelPlay/adUnits/v1/1a2b3c4d",
            [{"mediationAdunitId": "rw00000000000001", "isPaused": False}],
        )
    ]
    cfg = config(app_dir)
    assert cfg["levelplay"]["androidAppKey"].startswith("new")
    assert set(cfg["levelplay"]["android"]) == {"rewarded", "interstitial"}
    assert set(cfg["levelplay"]["ios"]) == {"rewarded", "interstitial"}

    before = len(fake.writes())
    text = (app_dir / "Assets/Resources/studio_app.json").read_text(encoding="utf-8")
    code, out = run(app_dir, lp, create=True)
    assert code == 0 and "already up to date" in out
    assert len(fake.writes()) == before, "a second run creates nothing"
    assert (app_dir / "Assets/Resources/studio_app.json").read_text(encoding="utf-8") == text


def test_dry_run_writes_nothing(app_dir):
    fake = FakeLevelPlay()
    text = (app_dir / "Assets/Resources/studio_app.json").read_text(encoding="utf-8")
    code, out = run(app_dir, tool.LevelPlay(fake, SECRET, REFRESH), create=True, dry_run=True)
    assert fake.writes() == [] and "--dry-run" in out
    assert (app_dir / "Assets/Resources/studio_app.json").read_text(encoding="utf-8") == text


def test_max_pages_creates_missing_and_is_idempotent(app_dir):
    fake = FakeMax(page_size_units=tool.PAGE)
    mx = tool.Max(fake, MGMT)
    code, out = run(app_dir, mx, provider="max", create=True)
    assert code == 0, out
    gets = [c for c in fake.calls if c[0] == "GET"]
    assert [c[2] for c in gets] == ["limit=100&offset=0", "limit=100&offset=100"], "paginated"
    posts = [c[3] for c in fake.calls if c[0] == "POST"]
    assert {(p["platform"], p["ad_format"]) for p in posts} == {
        ("ios", "INTER"),
        ("android", "REWARD"),
        ("android", "INTER"),
    }, "the disabled android INTER and other apps' units are not reused"
    assert all(p["package_name"] == "com.example.puzzle" for p in posts)
    cfg = config(app_dir)
    assert cfg["max"]["ios"]["rewarded"] == "45de6aa565cf865f"
    n = len(fake.calls)
    code, out = run(app_dir, mx, provider="max", create=True)
    assert code == 0 and "already up to date" in out
    assert not [c for c in fake.calls[n:] if c[0] == "POST"]


def test_secrets_are_never_printed(app_dir, monkeypatch, capsys):
    def leaky(secrets, timeout=30.0):
        def call(method, url, headers, body):
            # an API that echoes the credentials back in its error
            raise tool.OnboardError(f"{method} {url}: HTTP 401 bad key {SECRET} / {REFRESH}")

        return call

    monkeypatch.setattr(tool, "http_transport", leaky)
    monkeypatch.setenv("LEVELPLAY_SECRET_KEY", SECRET)
    monkeypatch.setenv("LEVELPLAY_REFRESH_TOKEN", REFRESH)
    code = tool.main(["--manifest", str(app_dir / "studio-app.json")])
    err = capsys.readouterr()
    assert code == 2
    assert SECRET not in err.err + err.out and REFRESH not in err.err + err.out
    assert "***" in err.err

    fake = FakeLevelPlay()
    monkeypatch.setattr(tool, "http_transport", lambda secrets, timeout=30.0: fake)
    code = tool.main(["--manifest", str(app_dir / "studio-app.json"), "--create"])
    out = capsys.readouterr()
    assert code == 0
    written = (app_dir / "Assets/Resources/studio_app.json").read_text(encoding="utf-8")
    for s in (SECRET, REFRESH, "BEARER-TOKEN"):
        assert s not in out.out + out.err and s not in written


def test_without_credentials_it_explains_and_prints_app_ads_txt(app_dir, monkeypatch, capsys):
    for name in ("LEVELPLAY_SECRET_KEY", "LEVELPLAY_REFRESH_TOKEN", "APPLOVIN_MANAGEMENT_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert tool.main(["--manifest", str(app_dir / "studio-app.json")]) == 2
    out = capsys.readouterr().out
    assert "LEVELPLAY_SECRET_KEY and LEVELPLAY_REFRESH_TOKEN not set" in out
    assert "https://example.com/app-ads.txt" in out and "text/plain" in out
    assert tool.main(["--manifest", str(app_dir / "studio-app.json"), "--provider", "max"]) == 2
    out = capsys.readouterr().out
    assert "applovin.com, <id from Account > General > App-ads.txt Info>, DIRECT" in out
    lines = tool.app_ads_txt(tool.load_manifest(app_dir / "studio-app.json"), "max", "abc123")
    assert "  applovin.com, abc123, DIRECT" in lines


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"name": ""}, "'name' is required"),
        ({"platforms": {"windows": {}}}, "ios and/or android"),
        ({"platforms": {"ios": {"bundle_id": " "}}}, "platforms.ios.bundle_id"),
        ({"ad_formats": ["rewarded", "offerwall"]}, "unknown ad_formats"),
    ],
)
def test_manifest_errors_are_clear(tmp_path, patch, message):
    m = {**fixture("studio-app.json"), **patch}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(m))
    with pytest.raises(tool.OnboardError, match=message):
        tool.load_manifest(p)
