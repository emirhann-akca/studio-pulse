#!/usr/bin/env bash
# Compiles the ad SDK bridges, the ads package and its editor scripts against the REAL vendor
# sources, once per platform define set, warnings as errors for this package's code:
# AppLovin MAX 8.6.6 (unity.packages.applovin.com), Unity LevelPlay 9.6.0 and 8.9.0
# (download.packages.unity.com; the provider needs 9.5+), Google Mobile Ads 11.7.0 AdValue and the AppsFlyer 7.0.41
# upm branch (GitHub). The vendor files are fetched once into $PULSE_VENDOR_CACHE
# (default ~/.cache/studio-pulse-vendor) and only compiled, never run.
#   unity/dotnet/ads-compile.sh
# UNITY_CONTENTS=/Applications/Unity/Hub/Editor/<version>/Unity.app/Contents overrides the lookup.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
if [ -z "${UNITY_CONTENTS:-}" ]; then
  UNITY_CONTENTS="$(ls -d /Applications/Unity/Hub/Editor/*/Unity.app/Contents 2>/dev/null | sort -V | tail -1)"
fi
editor_root="$(cd "$UNITY_CONTENTS/../.." && pwd)"
S="$UNITY_CONTENTS/Resources/Scripting"
mono="$S/MonoBleedingEdge/bin/mono"
csc="$S/MonoBleedingEdge/lib/mono/4.5/csc.exe"
ns="$S/NetStandard/ref/2.1.0/netstandard.dll"
engine="$S/Managed/UnityEngine"
api="$S/UnityReferenceAssemblies/unity-4.8-api"
pkg="$repo/unity/com.studio.pulse"
out="${PULSE_MONO_OUT:-$repo/unity/dotnet/bin-mono}/ads"
V="${PULSE_VENDOR_CACHE:-$HOME/.cache/studio-pulse-vendor}"
mkdir -p "$out" "$V"

fetch_tgz() { # url dir
  [ -d "$2" ] && return 0
  mkdir -p "$2.tmp" && curl -fsSL "$1" | tar xz -C "$2.tmp" && mv "$2.tmp" "$2"
}
fetch_tgz https://unity.packages.applovin.com/com.applovin.mediation.ads/-/com.applovin.mediation.ads-8.6.6.tgz "$V/max-8.6.6"
fetch_tgz https://download.packages.unity.com/com.unity.services.levelplay/-/com.unity.services.levelplay-9.6.0.tgz "$V/levelplay-9.6.0"
fetch_tgz https://download.packages.unity.com/com.unity.services.levelplay/-/com.unity.services.levelplay-8.9.0.tgz "$V/levelplay-8.9.0"
fetch_tgz https://codeload.github.com/AppsFlyerSDK/appsflyer-unity-plugin/tar.gz/409b10a5dd0d16795c1b4cf28e0412698e1e0f06 "$V/appsflyer-7.0.41"
if [ ! -f "$V/gma-11.7.0/AdValue.cs" ]; then
  mkdir -p "$V/gma-11.7.0"
  curl -fsSL https://raw.githubusercontent.com/googleads/googleads-mobile-unity/v11.7.0/source/plugin/Assets/GoogleMobileAds/Api/Core/AdValue.cs -o "$V/gma-11.7.0/AdValue.cs"
fi
max="$V/max-8.6.6/package/Scripts"
af="$(ls -d "$V"/appsflyer-7.0.41/*/ | head -1)"

# MAX's Editor-only scripts use uGUI, a package that cannot be compiled outside Unity: the Editor
# variants run only with UGUI_DLL=<any Unity 6 project>/Library/ScriptAssemblies/UnityEngine.UI.dll.
ugui="${UGUI_DLL:-}"

erefs=(); for d in "$engine"/UnityEngine*.dll; do erefs+=("-r:$d"); done
edrefs=(); for d in "$engine"/UnityEditor*.dll; do edrefs+=("-r:$d"); done
edrefs+=("-r:$editor_root/PlaybackEngines/iOSSupport/UnityEditor.iOS.Extensions.Xcode.dll"
         "-r:$editor_root/PlaybackEngines/AndroidPlayer/UnityEditor.Android.Extensions.dll")
fwk=("-r:$api/mscorlib.dll" "-r:$api/System.dll" "-r:$api/System.Core.dll" "-r:$api/System.Xml.dll" "-r:$api/System.Xml.Linq.dll" "-r:$api/Facades/netstandard.dll")
base="UNITY_5_3_OR_NEWER;UNITY_2019_4_OR_NEWER;UNITY_2020_3_OR_NEWER;UNITY_2021_3_OR_NEWER;UNITY_2022_3_OR_NEWER;UNITY_2023_1_OR_NEWER;UNITY_6000_0_OR_NEWER"
vendor_nowarn="-nowarn:0618,0612,0414,0649,0169,0067,0168,0219,1998,4014,8632,0108,0114"
cc() { "$mono" "$csc" -nologo -nostdlib -noconfig -t:library -langversion:9 "$@"; }

cc -warnaserror -out:"$out/Studio.Pulse.Core.dll" -r:"$ns" "$pkg"/Runtime/Core/*.cs
plats=("UNITY_IOS;UNITY_IPHONE" "UNITY_ANDROID" "UNITY_STANDALONE_OSX")
if [ -n "$ugui" ] && [ -f "$ugui" ]; then
  erefs+=("-r:$ugui")
  plats+=("UNITY_EDITOR;UNITY_IOS;UNITY_IPHONE" "UNITY_EDITOR;UNITY_ANDROID")
else
  echo "UGUI_DLL not set: skipping the Editor variants (runtime only)"
fi
for plat in "${plats[@]}"; do
  d="$base;$plat"; o="$out/$(echo "$plat" | tr ';' '_')"; mkdir -p "$o"
  R=(-r:"$ns" -r:"$out/Studio.Pulse.Core.dll" "${erefs[@]}")
  case "$plat" in UNITY_EDITOR*) R+=("${edrefs[@]}");; esac
  cc $vendor_nowarn -define:"$d" -out:"$o/MaxSdk.Scripts.dll" "${R[@]}" $(find "$max" -name "*.cs" -not -path "*/IntegrationManager/*")
  cc $vendor_nowarn -define:"$d" -out:"$o/Unity.LevelPlay.dll" "${R[@]}" $(find "$V/levelplay-9.6.0/package/Runtime" -name "*.cs")
  cc $vendor_nowarn -define:"$d" -out:"$o/Unity.LevelPlay.8.9.dll" "${R[@]}" $(find "$V/levelplay-8.9.0/package/Runtime" -name "*.cs")
  cc -define:"$d" -out:"$o/GoogleMobileAds.Core.dll" -r:"$ns" "$V/gma-11.7.0/AdValue.cs"
  cc $vendor_nowarn -define:"$d" -out:"$o/AppsFlyer.dll" "${R[@]}" $(find "$af" -maxdepth 1 -name "*.cs")
  cc -warnaserror -define:"$d" -out:"$o/Studio.Pulse.dll" "${R[@]}" "$pkg"/Runtime/*.cs
  R2=("${R[@]}" -r:"$o/Studio.Pulse.dll")
  cc -warnaserror -define:"$d;STUDIO_PULSE_MAX" -out:"$o/Studio.Pulse.AppLovinMax.dll" "${R2[@]}" -r:"$o/MaxSdk.Scripts.dll" "$pkg"/Runtime/AdBridges/AppLovinMax/*.cs
  cc -warnaserror -define:"$d;STUDIO_PULSE_LEVELPLAY" -out:"$o/Studio.Pulse.LevelPlay.dll" "${R2[@]}" -r:"$o/Unity.LevelPlay.dll" "$pkg"/Runtime/AdBridges/LevelPlay/*.cs
  cc -warnaserror -define:"$d;STUDIO_PULSE_LEVELPLAY" -out:"$o/Studio.Pulse.LevelPlay.8.9.dll" "${R2[@]}" -r:"$o/Unity.LevelPlay.8.9.dll" "$pkg"/Runtime/AdBridges/LevelPlay/*.cs
  cc -warnaserror -define:"$d;STUDIO_PULSE_GMA" -out:"$o/Studio.Pulse.GoogleMobileAds.dll" "${R2[@]}" -r:"$o/GoogleMobileAds.Core.dll" "$pkg"/Runtime/AdBridges/GoogleMobileAds/*.cs
  cc -warnaserror -define:"$d" -out:"$o/Studio.Pulse.Ads.dll" "${R2[@]}" "$pkg"/Runtime/Ads/*.cs
  R3=("${R2[@]}" -r:"$o/Studio.Pulse.Ads.dll")
  cc -warnaserror -define:"$d;STUDIO_PULSE_MAX" -out:"$o/Studio.Pulse.Ads.Max.dll" "${R3[@]}" -r:"$o/MaxSdk.Scripts.dll" "$pkg"/Runtime/Ads/Max/*.cs
  cc -warnaserror -define:"$d;STUDIO_PULSE_APPSFLYER" -out:"$o/Studio.Pulse.Ads.AppsFlyer.dll" "${R3[@]}" -r:"$o/AppsFlyer.dll" "$pkg"/Runtime/Ads/AppsFlyer/*.cs
  cc -warnaserror -define:"$d;STUDIO_PULSE_LEVELPLAY_ADS" -out:"$o/Studio.Pulse.Ads.LevelPlay.dll" "${R3[@]}" -r:"$o/Unity.LevelPlay.dll" "$pkg"/Runtime/Ads/LevelPlay/*.cs
  # the ATT variant of the ads assembly (iOS builds that define STUDIO_PULSE_ATT)
  cc -warnaserror -define:"$d;STUDIO_PULSE_ATT" -out:"$o/Studio.Pulse.Ads.Att.dll" "${R2[@]}" "$pkg"/Runtime/Ads/*.cs
  cc -warnaserror -define:"$d" -out:"$o/AdsQuickStart.dll" "${R3[@]}" "$pkg"/Samples~/AdsQuickStart/*.cs
  echo "compiled runtime assemblies ($plat)"
  case "$plat" in UNITY_EDITOR*)
    E=("${fwk[@]}" -r:"$out/Studio.Pulse.Core.dll" "${erefs[@]}" "${edrefs[@]}")
    cc $vendor_nowarn -define:"$d" -out:"$o/MaxSdk.Scripts.IntegrationManager.Editor.dll" "${E[@]}" -r:"$o/MaxSdk.Scripts.dll" \
      $(find "$max/IntegrationManager/Editor" -name "*.cs")
    cc -warnaserror -define:"$d" -out:"$o/Studio.Pulse.Editor.dll" "${E[@]}" "$pkg"/Editor/*.cs
    cc -warnaserror -define:"$d" -out:"$o/Studio.Pulse.Ads.Editor.dll" "${E[@]}" "$pkg"/Editor/Ads/*.cs
    cc -warnaserror -define:"$d;STUDIO_PULSE_MAX" -out:"$o/Studio.Pulse.Ads.Max.Editor.dll" "${E[@]}" -r:"$o/Studio.Pulse.Ads.Editor.dll" \
      -r:"$o/MaxSdk.Scripts.dll" -r:"$o/MaxSdk.Scripts.IntegrationManager.Editor.dll" "$pkg"/Editor/Ads/Max/*.cs
    echo "compiled editor assemblies ($plat)";;
  esac
done
echo "ads-compile: all assemblies compiled"
