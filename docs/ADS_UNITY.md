# Ads for Unity games (`Studio.Pulse.Ads`, in `unity/com.studio.pulse`)

One config file turns on analytics, ads and attribution for any of the studio's Unity
games. The game supplies only a small adapter from its own ad buttons to `StudioAds`.
Everything defaults to empty: **an empty `studio_app.json` starts nothing, makes no network
call and shows no ad**, so a build without keys behaves like a build without the SDKs.

| Part | Assembly | Compiled when |
|---|---|---|
| Config, `StudioApp.Start`, `StudioAds`, ATT and Google UMP bridges | `Studio.Pulse.Ads` | always (ATT / UMP native parts only with `STUDIO_PULSE_ATT` / `STUDIO_PULSE_UMP`) |
| Unity LevelPlay provider (first choice) | `Studio.Pulse.Ads.LevelPlay` | `com.unity.services.levelplay` 9.5.0 to < 10 |
| AppLovin MAX provider | `Studio.Pulse.Ads.Max` | `com.applovin.mediation.ads` 8.0.0+ |
| AppsFlyer MMP | `Studio.Pulse.Ads.AppsFlyer` | `appsflyer-unity-plugin` 7.0.0+ |
| Ad revenue bridges (no game code) | `Studio.Pulse.LevelPlay`, `Studio.Pulse.AppLovinMax` | LevelPlay 8.9.0 to < 10, MAX 8.0.0+ |
| Build steps (release keys, MAX settings, Android manifest, iOS ATT text) | `Studio.Pulse.Ads.Editor`, `Studio.Pulse.Ads.Max.Editor` | Editor |

UPM installs set the symbols through the asmdefs' `versionDefines`. SDKs imported as a
`.unitypackage` into `Assets/` get the same symbols from `Editor/PulseAdSdkDefines.cs`
(`STUDIO_PULSE_MAX`, `STUDIO_PULSE_LEVELPLAY`, `STUDIO_PULSE_LEVELPLAY_ADS`,
`STUDIO_PULSE_GMA`, `STUDIO_PULSE_APPSFLYER`; menu *Tools > Studio Pulse > Refresh ad SDK
defines*), which also removes them when the SDK goes away.

## Use

1. Package Manager: the Studio Pulse git URL (docs/SDK_UNITY.md), then the ad SDK: **Ads
   Mediation** (`com.unity.services.levelplay`) from the Unity Registry. Opening the Editor
   once lets LevelPlay install its native SDK description (`Assets/LevelPlay/Editor/*.xml`) and
   the dependency resolver; commit those files.
2. Copy `Samples~/AdsQuickStart/studio_app.json` to `Assets/Resources/studio_app.json`.
3. At startup: `StudioApp.Start();`, or the one line that also wires the game in:
   `StudioApp.Launch(started: () => MyAnalytics.Bind(), adsReady: () => MyRewards.Use(new MyAdsBridge()));`
   (`started` always runs after the start, `adsReady` only when an ad provider is attached, so a
   keyless build shows no ad button; never throws, runs once). Ad buttons: `if (StudioAds.RewardedReady) ...`,
   `StudioAds.ShowRewarded(placement, granted => ...)`, `StudioAds.ShowInterstitial(placement, then)`.
   Callbacks run on the main thread; `StudioAds.AvailabilityChanged` fires when an ad loads.
4. iOS with LevelPlay: add `STUDIO_PULSE_ATT` to the iOS Scripting Define Symbols (it compiles
   the tracking prompt, `Plugins/iOS/Ads/StudioPulseAtt.mm`; apps without ads leave it out).
   GDPR / UK consent with Google UMP: copy `Samples~/AdsQuickStart/StudioPulseUmpDependencies.xml`
   into `Assets/` (UMP SDK: CocoaPods `GoogleUserMessagingPlatform` 3.1.0, Maven
   `com.google.android.ump:user-messaging-platform` 4.0.0), add `STUDIO_PULSE_UMP` for iOS and
   Android, fill `ump.iosAppId` / `ump.androidAppId` (AdMob app ids) and publish a GDPR message in
   AdMob > Privacy & messaging. Offer `StudioAds.ShowPrivacyOptions` from the game's settings when
   `StudioAds.PrivacyOptionsRequired`.
5. Fill the keys (below), build. Every build logs `[Studio.Ads] <platform> build: analytics
   ..., ads ..., MMP ...` and any half-filled section.

## The config, `Assets/Resources/studio_app.json`

| Key | From | Effect |
|---|---|---|
| `pulse.collectorUrl`, `pulse.writeKey` | `pulse collect` (COLLECTOR.md) | `Pulse.Init` (https only in store builds) |
| `privacy.privacyPolicyUrl`, `termsOfServiceUrl` | the app's pages | MAX consent flow; documentation |
| `privacy.attUsageDescription[]` (`lang`, `text`) | written by you, `en` required | `NSUserTrackingUsageDescription`, localized |
| `privacy.requestAtt` | | LevelPlay: ask ATT before `LevelPlay.Init` |
| `privacy.gdprConsent` | `""`, `"granted"`, `"denied"` | passed before init when the game has no CMP |
| `privacy.childDirected` | | LevelPlay: `SetCOPPA(true)`; MAX is not started at all (AppLovin terms) |
| `ump.iosAppId`, `ump.androidAppId` | AdMob > Apps > App settings (`ca-app-pub-…~…`) | Google UMP consent form before LevelPlay (iOS: written to Info.plist `GADApplicationIdentifier`; Android: passed at run time) |
| `ump.debugGeography`, `ump.testDeviceIds` | `eea` / `us_state` / `other`; ids UMP logs | development builds only: force the form's region |
| `levelplay.iosAppKey`, `androidAppKey` | LevelPlay platform > Apps | LevelPlay on that platform |
| `levelplay.{ios,android}.{rewarded,interstitial,banner}` | LevelPlay platform > Ad units | ad units |
| `max.sdkKey`, `max.{ios,android}.*` | AppLovin dashboard > Keys, MAX > Ad units | MAX, only where LevelPlay is empty |
| `appsflyer.devKey`, `iosAppId` | AppsFlyer dashboard | MMP after the consent / ATT step |
| `pacing.*` | | optional extra limits (0 = none) |

`tools/ads_onboard.py` (below) writes the LevelPlay or MAX ids. A provider starts only when
its key **and** at least one ad unit id for the platform are set; LevelPlay wins when both are.

## What happens at `StudioApp.Start()`

1. `Pulse.Init` when the collector is configured.
2. LevelPlay: the Google UMP consent form when an AdMob app id is set (UMP shows it only where
   required: EEA, UK, Switzerland; an unanswered request gives up after 12 s, never while the form is
   on screen), the ATT prompt on iOS (once; answered users are not asked again), then `SetCOPPA` /
   `SetGDPRConsent` / `SetCCPA` (when set) and `LevelPlay.Init(appKey)`; after
   `OnInitSuccess` the rewarded and interstitial ads are created and loaded. Failed loads
   retry after 2, 4 ... 64 s; ads reload after they close or fail to show. The reward is
   granted only by `OnAdRewarded`, which may arrive after `OnAdClosed`: `ShowRewarded` waits
   up to 2 s for it. GDPR consent collected by Google UMP or a CMP that writes Google
   Additional Consent is read by LevelPlay itself (LevelPlay SDK 7.7.0+).
   MAX: `InitializeSdk` runs AppLovin's Terms and Privacy Policy flow (ATT, UMP); same load,
   retry and reward rules (AppLovin's recommended `2^min(6, n)` s).
3. The MMP starts after that step, so it sees the ATT answer and the TCF string.
4. Every step is an `ad_event` (`request`, `fill`, `show`, `click`, `reward`, `fail`); revenue
   arrives as `ad_impression` through the bridges, and the same stream goes to the MMP
   (`PulseAdBridge.Impression`, AppsFlyer `logAdRevenue`).

## Build steps

- **Android, ads off**: the ad SDKs' start-up ContentProviders (AppLovin's reads the
  advertising id at app start even if MAX never starts; ironSource's lifecycle trackers) and
  `AD_ID` get `tools:node="remove"` in the launcher manifest. **LevelPlay on**: `AD_ID` declared.
- **iOS, ads on**: `NSUserTrackingUsageDescription` from `privacy.attUsageDescription`
  (English in `Info.plist`, the other languages as `<lang>.lproj/InfoPlist.strings`; for MAX
  only the languages MAX does not localize itself).
- **MAX**: `studio_app.json` is copied into `AppLovinSettings` (SDK key; Quality Service off
  without a key) and `ProjectSettings/AppLovinInternalSettings.json` (consent flow on only with
  a key, a privacy URL and an English ATT text), so the Integration Manager never has to be
  edited by hand and an empty config never fails the build.

## Release keys (`StudioAppRelease`)

The game's `Assets/Resources/studio_app.json` can stay **keyless** in its repository, so
development, test and simulator builds start nothing. The ids live in the game's
`studio-app.json` (the manifest below, next to the Unity project) and the collector key in the
studio's `<state>/sdk/<label>.env` (deploy/cloudrun/make_keys.py; never in a repository). A
build step copies them in and writes the file back byte for byte when the build ends:

| From | Into `studio_app.json` |
|---|---|
| `networks.levelplay.{ios,android}.app_key`, `.ad_units.*` | `levelplay` app keys and ad units (a platform is replaced as a whole) |
| `networks.ump.{ios,android}_app_id` | `ump.iosAppId`, `ump.androidAppId` |
| `networks.pulse.collectorUrl` / `writeKey`, else the `.env` (`PULSE_COLLECTOR_URL`, `PULSE_WRITE_KEY`) | `pulse.collectorUrl`, `pulse.writeKey` |

| Manifest key / variable | |
|---|---|
| `release_inject` | `store` (default): every non-development build; `manual`: only builds whose script calls `StudioAppRelease.BuildPlayer(options, ads)` (or `Apply` / `Restore`) |
| `pulse_label` | the key label; default: the `name` in lower-case ASCII (`Acorn Mart` -> `acornmart`) |
| `pulse_env` | another `.env` (relative to the manifest, `~` allowed) |
| `STUDIO_APP_MANIFEST` | the manifest's path; default: the project folder and up to three folders above it, not above the repository root |
| `STUDIO_PULSE_SDK_ENV` | the `.env`'s path (wins over `pulse_env`) |
| `STUDIO_APP_RELEASE=off` | no automatic step (CI) |

The step runs before `StudioAppBuild` (so its log and iOS / Android steps see the ids) and is
undone after every other post-process step, by `BuildPlayer`'s `finally`, when the Editor quits
and, after a crash, when the Editor loads again (a copy waits in `Library/StudioPulse/`). The log
names the collector's host, never the key. `StudioAppRelease.ResolveAndroidDependencies` runs
EDM4U's Android resolver in batchmode (`-executeMethod`, exit code 0 / 1).

## Onboarding an app: `tools/new_game.py` and `tools/ads_onboard.py`

`tools/new_game.py LABEL --name ... --ios ... --android ... --manifest <game>/studio-app.json`
does a new game in one go: the collector write key bound to its bundle ids (make_keys.py; Secret
Manager `pulse-write-keys` gets a new version and the Cloud Run service a revision pinned to it;
`<state>/sdk/<label>.env` for the release build; `--project`, default `grich-pulse`, is passed as
`--project` and `CLOUDSDK_CORE_PROJECT`), the LevelPlay app and ad units (below) and the game's
`studio-app.json`. Without `--apply` it only prints the plan and changes nothing. Step by step,
in Turkish: docs/YENI_OYUN_KURULUM.md.

Standard library only. A per-app manifest (kept in the game's repository) names the bundle
ids, store ids, formats and the config path; the tool lists the app and its ad units on the
ad platform, creates what is missing with `--create`, writes the ids into the config and prints
the `app-ads.txt` instructions. Re-running changes nothing that is already right.

```bash
LEVELPLAY_SECRET_KEY=... LEVELPLAY_REFRESH_TOKEN=... \
  python tools/ads_onboard.py --manifest studio-app.json --create      # LevelPlay
APPLOVIN_MANAGEMENT_KEY=... python tools/ads_onboard.py --manifest studio-app.json --provider max
```

| | LevelPlay (read 15 Nov 2026) | AppLovin MAX (read 15 Nov 2026) |
|---|---|---|
| Credentials | Secret Key + Refresh Token (platform: My Account) -> `GET https://platform.ironsrc.com/partners/publisher/auth`, bearer token valid 24 h | Management Key (Account > General > Keys), `Api-Key` header |
| App | `GET/POST .../partners/publisher/applications/v6` (a live app is created from its store URL, taxonomy, COPPA flag) | created with the first ad unit |
| Ad units | `GET/POST/PUT https://platform.ironsrc.com/levelPlay/adUnits/v1/{appKey}`; rewarded units need a reward name and amount; paused units are unpaused with `--create` | `GET https://o.applovin.com/mediation/v1/ad_units`, `POST .../ad_unit` (one active unit per app, platform and format) |
| Revenue | Reporting API `GET https://platform.ironsrc.com/levelPlay/reporting/v1` | Reporting API (UA connector, OPERATIONS.md) |
| Networks (e.g. Unity Ads as a bidder) | read only: `GET https://platform.ironsrc.com/levelPlay/network/instances/v4/{appKey}`; missing ones printed as a checklist from the manifest's `networks` (game id, placement ids). They are set in the LevelPlay platform (Mediation > Setup > SDK Networks), not in the app; the app only needs the adapter, which the LevelPlay package installs | not applicable |
| app-ads.txt | lines copied from the LevelPlay platform (no API for them was found) | `applovin.com, <id>, DIRECT` from Account > General > App-ads.txt Info |

`app-ads.txt` goes to the root of the developer website (`text/plain`, UTF-8): for Google Play
the website in the store listing, for the App Store the domain of the privacy policy URL.

## Privacy labels

With ads on, the App Store label and the Play Data safety form change (advertising data,
device ids, usage data, coarse location from the IP for the ad networks; "used for tracking"
on iOS when ATT is granted). The exact answers depend on the networks mediated; LevelPlay and
AppLovin publish theirs. Analytics alone: PRIVACY.md.

## Not verified on a device

Compiled with warnings as errors against the vendors' own sources (AppLovin MAX 8.6.6, Unity
LevelPlay 9.6.0 and 8.9.0, Google Mobile Ads 11.7.0 `AdValue`, AppsFlyer 7.0.41) for iOS,
Android, Editor and standalone: `unity/dotnet/ads-compile.sh` (set `UGUI_DLL` to a Unity 6
project's `Library/ScriptAssemblies/UnityEngine.UI.dll` for the Editor variants). The ATT
plug-in passes `clang -fsyntax-only` for iOS. The rules without Unity (mapping, de-duplication,
config decisions, retry, pacing, the release keys) are unit tests in `unity/dotnet`. The release
keys step ran in Unity 6000.3.23f1 (EditMode tests and four iOS player builds, 20 Nov 2026); the
ad providers have not run in the Editor, in a player build or with real ad accounts here (Acorn
Mart's own checks: its docs/YAYIN-1.0.1.md).
