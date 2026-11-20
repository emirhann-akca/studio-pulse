#!/usr/bin/env python3
"""Sets up a new game in one command: collector key, LevelPlay app and ad units, studio-app.json.

    python3 tools/new_game.py kupdunyalari --name "Küp Dünyaları" \\
        --ios com.emirhan.kupdunya --android com.emirhan.kupdunya \\
        --manifest ~/kup-dunya/studio-app.json             # plan: prints, changes nothing
    python3 tools/new_game.py ... --apply                  # does it

1. Collector write key, bound to the game's bundle ids (deploy/cloudrun/make_keys.py: the APPS
   logic of bootstrap.sh, without rebuilding the image). <state>/collector.env gets the key (keys
   already there are kept), Secret Manager pulse-write-keys a new version, the Cloud Run service
   a new revision pinned to that version (the collector reads its keys when it starts), and
   <state>/sdk/<label>.env the URL + key the game's release build reads (StudioAppRelease). The
   Google Cloud project is --project (default grich-pulse), given to every gcloud call as
   --project and CLOUDSDK_CORE_PROJECT (the machine's default project may be another one).
2. LevelPlay app and ad units (tools/ads_onboard.py), when LEVELPLAY_SECRET_KEY and
   LEVELPLAY_REFRESH_TOKEN are set: the plan lists them (read only), --apply creates what is
   missing. Their ids go into the manifest's networks.levelplay.
3. The game's studio-app.json (docs/YENI_OYUN_KURULUM.md): printed by the plan, written by
   --apply; an existing file is merged (ids already in it are kept unless step 2 found others).

Without --apply nothing changes anywhere: no file, no secret, no service, no ad platform. Keys are
never printed (only their key id, the hash prefix the collector stores). Standard library only.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


make_keys = _load("make_keys", ROOT / "deploy" / "cloudrun" / "make_keys.py")
ads_onboard = _load("ads_onboard", ROOT / "tools" / "ads_onboard.py")

DEFAULT_PROJECT = "grich-pulse"
DEFAULT_REGION = "us-central1"
DEFAULT_SERVICE = "pulse-collector"
SECRET = "pulse-write-keys"

# (argv, stdin) -> stdout; raises NewGameError when the command fails
Runner = Callable[[list[str], str | None], str]


class NewGameError(Exception):
    """A problem the user can fix; printed without a traceback."""


def user_state_dir(env: dict[str, str] | None = None, platform: str | None = None) -> Path:
    """studio-pulse's per-user folder (src/studio_pulse/schedule.py user_state_dir)."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get("PULSE_HOME"):
        return Path(env["PULSE_HOME"])
    if platform == "win32":
        return Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "studio-pulse"
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "studio-pulse"
    return Path(env.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "studio-pulse"


def gcloud_runner(project: str) -> Runner:
    env = {**os.environ, "CLOUDSDK_CORE_PROJECT": project}

    def run(argv: list[str], stdin: str | None) -> str:
        try:
            p = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                check=False,
            )
        except OSError as e:
            raise NewGameError(f"{argv[0]}: {e}") from None
        if p.returncode != 0:
            raise NewGameError(f"{' '.join(argv[:4])} ...: {p.stderr.strip()[:400]}")
        return p.stdout.strip()

    return run


# ---------------------------------------------------------------------------- manifest

HELP = (
    "Oyunun kurulum dosyası (studio-pulse docs/YENI_OYUN_KURULUM.md). tools/new_game.py ve "
    "tools/ads_onboard.py yazar; Unity'deki Studio Pulse paketi (StudioAppRelease) yayın "
    "derlemesinde buradaki kimlikleri Assets/Resources/studio_app.json'a yazar ve derleme bitince "
    "geri alır. Kimlikler herkese açıktır; toplayıcı yazma anahtarı BURADA DEĞİL, stüdyonun "
    "yerel <durum>/sdk/<pulse_label>.env dosyasında."
)
UMP_HELP = (
    "AdMob uygulama kimlikleri (ca-app-pub-...~...): AdMob > Apps > App settings. Boşsa AB/BK "
    "onay formu (Google UMP) çıkmaz."
)
PULSE_HELP = (
    "Boş kalır: yayın derlemesi toplayıcı adresini ve yazma anahtarını stüdyonun yerel "
    "<durum>/sdk/<pulse_label>.env dosyasından okur."
)


def build_manifest(existing: dict[str, Any] | None, a: argparse.Namespace) -> dict[str, Any]:
    """The game's studio-app.json: `existing` (kept) with what the command line gives."""
    m: dict[str, Any] = copy.deepcopy(existing) if existing else {"_help": HELP}
    m["name"] = a.name
    m["pulse_label"] = a.label
    plats: dict[str, Any] = m.setdefault("platforms", {})
    if a.ios:
        ios = plats.setdefault("ios", {})
        ios["bundle_id"] = a.ios
        if a.app_store_id:
            ios["app_store_id"] = a.app_store_id
    if a.android:
        plats.setdefault("android", {})["package_name"] = a.android
    if a.formats:
        m["ad_formats"] = a.formats
    m.setdefault("ad_formats", ["rewarded", "interstitial"])
    m.setdefault("rewarded_item", {"name": "Coins", "amount": 1})
    if a.taxonomy:
        m["levelplay_taxonomy"] = a.taxonomy
    m.setdefault("levelplay_taxonomy", "puzzle")
    m.setdefault("coppa", False)
    if a.privacy_url:
        m["privacy_policy_url"] = a.privacy_url
    if a.website:
        m["developer_website"] = a.website
    m.setdefault("unity_config", "")
    m.setdefault("release_inject", "store")
    nets: dict[str, Any] = m.setdefault("networks", {})
    lp: dict[str, Any] = nets.setdefault("levelplay", {})
    for p in plats:
        app = lp.setdefault(p, {})
        app.setdefault("app_key", "")
        units = app.setdefault("ad_units", {})
        for f in m["ad_formats"]:
            units.setdefault(f, "")
    ump = nets.setdefault("ump", {"_help": UMP_HELP})
    ump.setdefault("ios_app_id", "")
    ump.setdefault("android_app_id", "")
    pulse = nets.setdefault("pulse", {"_help": PULSE_HELP})
    pulse.setdefault("collectorUrl", "")
    pulse.setdefault("writeKey", "")
    return m


def put_levelplay(m: dict[str, Any], values: dict[Any, str]) -> list[str]:
    """ads_onboard.run_levelplay's values -> networks.levelplay; returns what changed."""
    lp = m.setdefault("networks", {}).setdefault("levelplay", {})
    changed = []
    for k, v in sorted(values.items(), key=lambda kv: str(kv[0])):
        if isinstance(k, tuple):
            platform, fmt = k
            target, field = lp.setdefault(platform, {}).setdefault("ad_units", {}), fmt
            label = f"networks.levelplay.{platform}.ad_units.{fmt}"
        else:
            platform = k[: -len("AppKey")]
            target, field = lp.setdefault(platform, {}), "app_key"
            label = f"networks.levelplay.{platform}.app_key"
        if target.get(field) != v:
            target[field] = v
            changed.append(label)
    return changed


# ---------------------------------------------------------------------------- steps


def collector_step(
    label: str,
    app_ids: list[str],
    state: Path,
    project: str,
    region: str,
    service: str,
    apply: bool,
    run: Runner,
    fetch: Callable[[str], str],
    out: Callable[[str], None],
) -> None:
    env_path = state / "collector.env"
    gc = ["--project", project]
    out(f"1/3 Toplayıcı yazma anahtarı: {label} -> {', '.join(app_ids)}")
    out(f"    GCP projesi {project} (gcloud'a --project ve CLOUDSDK_CORE_PROJECT={project})")
    try:
        make_keys.cmd_keys(env_path, [f"{label}={'|'.join(app_ids)}"], dry=not apply)
    except make_keys.Problem as e:
        raise NewGameError(f"make_keys: {e}") from None
    if not apply:
        out(f"+ gcloud secrets versions add {SECRET} --data-file=- --project {project}")
        out(f"    (değer {env_path} içindeki PULSE_WRITE_KEYS; komut satırına ve ekrana girmez)")
        out(
            f"+ gcloud run services update {service} --region {region} --project {project} "
            f"--update-secrets PULSE_WRITE_KEYS={SECRET}:<yeni sürüm>"
        )
        out("    (toplayıcı anahtarları açılışta okur: yeni revizyon yeni anahtarı tanır)")
        out(
            f"+ make_keys.py sdk {env_path} {state / 'sdk'}  ->  {state / 'sdk' / (label + '.env')}"
        )
        out("+ curl <toplayıcı>/health")
        return
    values, other = make_keys.read_env(env_path)
    keys = values.get("PULSE_WRITE_KEYS", "")
    try:
        current = run(
            ["gcloud", "secrets", "versions", "access", "latest", "--secret", SECRET, *gc], None
        )
    except NewGameError:
        current = None
        out(
            f"    {SECRET} okunamadı (toplayıcı hiç kurulmadıysa önce deploy/cloudrun/bootstrap.sh)"
        )
    if current == keys:
        out(f"    {SECRET} zaten güncel")
        version = run(
            ["gcloud", "secrets", "versions", "describe", "latest", "--secret", SECRET, *gc,
             "--format=value(name)"],
            None,
        )  # fmt: skip
    else:
        version = run(
            ["gcloud", "secrets", "versions", "add", SECRET, "--data-file=-", *gc,
             "--format=value(name)"],
            keys,
        )  # fmt: skip
        out(f"    {SECRET}: yeni sürüm {version.rsplit('/', 1)[-1]}")
    number = version.rsplit("/", 1)[-1]
    run(
        ["gcloud", "run", "services", "update", service, "--region", region, *gc,
         f"--update-secrets=PULSE_WRITE_KEYS={SECRET}:{number}"],
        None,
    )  # fmt: skip
    out(f"    {service}: yeni revizyon, {SECRET}:{number}")
    url = run(
        ["gcloud", "run", "services", "describe", service, "--region", region, *gc,
         "--format=value(status.url)"],
        None,
    )  # fmt: skip
    have = next((ln.partition("=")[2] for ln in other if ln.startswith("PULSE_COLLECTOR_URL=")), "")
    if url and have != url:
        text = env_path.read_text(encoding="utf-8").splitlines()
        text = [ln for ln in text if not ln.startswith("PULSE_COLLECTOR_URL=")]
        make_keys.write_private(env_path, "\n".join([*text, f"PULSE_COLLECTOR_URL={url}"]) + "\n")
    make_keys.cmd_sdk(env_path, state / "sdk")
    try:
        out(f"    {url}/health: {fetch(url + '/health')[:120]}")
    except (OSError, urllib.error.URLError) as e:
        out(f"    UYARI: {url}/health cevap vermedi ({e})")


def levelplay_step(
    manifest: dict[str, Any], apply: bool, client: Any, out: Callable[[str], None]
) -> tuple[dict[Any, str] | None, list[str]]:
    out("2/3 LevelPlay uygulaması ve reklam birimleri (tools/ads_onboard.py)")
    if client is None:
        out("    LEVELPLAY_SECRET_KEY ve LEVELPLAY_REFRESH_TOKEN yok: adım atlandı.")
        out("    (LevelPlay platformu > My Account; değerleri yalnız ortam değişkeninde tut)")
        return None, []
    values, problems = ads_onboard.run_levelplay(client, manifest, apply, out)
    for p in problems:
        out(f"    eksik: {p}")
    if problems and not apply:
        out("    --apply eksikleri kurar (uygulama için mağaza adresi gerekir)")
    return values, problems


def http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310 (the collector's https URL)
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------- main


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Yeni oyun: toplayıcı anahtarı + LevelPlay + studio-app.json (varsayılan: plan)."
    )
    ap.add_argument("label", help="anahtar etiketi: küçük harf, rakam, _ (ör. kupdunyalari)")
    ap.add_argument("--name", required=True, help="oyunun adı (LevelPlay'de ve birim adlarında)")
    ap.add_argument("--ios", help="iOS bundle id")
    ap.add_argument("--android", help="Android paket adı")
    ap.add_argument(
        "--app-store-id", help="App Store sayısal kimliği (LevelPlay iOS uygulaması için)"
    )
    ap.add_argument("--taxonomy", help="LevelPlay kategorisi (varsayılan puzzle)")
    ap.add_argument(
        "--formats", help="reklam biçimleri, virgüllü (varsayılan rewarded,interstitial)"
    )
    ap.add_argument("--privacy-url", help="gizlilik politikası adresi")
    ap.add_argument("--website", help="geliştirici sitesi (app-ads.txt bu alanın kökünde)")
    ap.add_argument(
        "--manifest", type=Path, default=Path("studio-app.json"), help="oyunun studio-app.json'ı"
    )
    ap.add_argument(
        "--project", default=DEFAULT_PROJECT, help=f"GCP projesi (varsayılan {DEFAULT_PROJECT})"
    )
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--service", default=DEFAULT_SERVICE)
    ap.add_argument("--skip-collector", action="store_true", help="toplayıcı adımını atla")
    ap.add_argument(
        "--apply", action="store_true", help="planı uygula (bulutta ve diskte değişiklik)"
    )
    a = ap.parse_args(argv)
    a.label = a.label.strip().lower()
    if not make_keys.LABEL.match(a.label):
        ap.error(f"{a.label!r}: etiket küçük harfle başlar; harf, rakam, _ (en çok 32)")
    if not a.ios and not a.android:
        ap.error("--ios ya da --android (en az biri)")
    for v in (a.ios, a.android):
        if v and not make_keys.APP_ID.match(v):
            ap.error(f"{v!r} bir bundle id / paket adı değil")
    if a.formats:
        a.formats = [f.strip() for f in a.formats.split(",") if f.strip()]
        bad = [f for f in a.formats if f not in ads_onboard.FORMATS]
        if bad:
            ap.error(f"bilinmeyen reklam biçimi {bad} (seçenekler {list(ads_onboard.FORMATS)})")
    return a


def main(
    argv: list[str] | None = None,
    *,
    run: Runner | None = None,
    client: Any = "env",
    state: Path | None = None,
    fetch: Callable[[str], str] = http_get,
    out: Callable[[str], None] = print,
) -> int:
    a = parse(sys.argv[1:] if argv is None else argv)
    state = state or user_state_dir()
    run = run or gcloud_runner(a.project)
    secrets: list[str] = []
    try:
        if client == "env":
            secrets = [os.environ.get(n, "").strip() for n in ads_onboard.ENV["levelplay"]]
            client = (
                ads_onboard.LevelPlay(ads_onboard.http_transport(secrets), *secrets)
                if all(secrets)
                else None
            )
        path = a.manifest.expanduser()
        existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        manifest = build_manifest(existing, a)
        ids = list(dict.fromkeys(v for v in (a.ios, a.android) if v))
        mode = "UYGULA" if a.apply else "PLAN (hiçbir şey değişmez; uygulamak için --apply)"
        out(f"Yeni oyun: {a.name} [{a.label}] {', '.join(ids)} -- {mode}")
        if a.skip_collector:
            out("1/3 Toplayıcı adımı atlandı (--skip-collector)")
        else:
            collector_step(
                a.label, ids, state, a.project, a.region, a.service, a.apply, run, fetch, out
            )
        values, problems = levelplay_step(manifest, a.apply, client, out)
        if values:
            for c in put_levelplay(manifest, values):
                out(f"    {c}")
        out(f"3/3 {path}")
        text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        if a.apply:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            out(f"    yazıldı ({'güncellendi' if existing else 'yeni'})")
        else:
            out("    yazılmadı; --apply ile yazılacak içerik:")
            for line in text.splitlines():
                out("    | " + line)
        out("Sıradaki (docs/YENI_OYUN_KURULUM.md): UMP kimlikleri (AdMob), app-ads.txt, ")
        out("  Unity paketleri, ödül yerleri, mağaza beyanları, gizlilik sayfası.")
        for line in ads_onboard.app_ads_txt(manifest, "levelplay", None):
            out("  " + line)
        return 1 if problems and a.apply else 0
    except (NewGameError, ads_onboard.OnboardError, OSError, ValueError) as e:
        print("hata: " + ads_onboard.mask(str(e), secrets), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
