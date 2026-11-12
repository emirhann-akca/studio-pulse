#!/usr/bin/env bash
# Compiles the Unity adapter (unity/com.studio.pulse/Runtime, UnityEngine side) against the
# UnityEngine module assemblies of an installed Editor, once per platform define set, with
# warnings as errors: proof that the package compiles without opening the Editor.
#   unity/dotnet/unity-compile.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
if [ -z "${UNITY_CONTENTS:-}" ]; then
  UNITY_CONTENTS="$(ls -d /Applications/Unity/Hub/Editor/*/Unity.app/Contents 2>/dev/null | sort -V | tail -1)"
fi
mono_dir="$UNITY_CONTENTS/Resources/Scripting/MonoBleedingEdge"
mono="$mono_dir/bin/mono"
csc="$mono_dir/lib/mono/4.5/csc.exe"
ns="$UNITY_CONTENTS/Resources/Scripting/NetStandard/ref/2.1.0/netstandard.dll"
engine="$UNITY_CONTENTS/Resources/Scripting/Managed/UnityEngine"
out="${PULSE_MONO_OUT:-$repo/unity/dotnet/bin-mono}"
mkdir -p "$out"
"$mono" "$csc" -nologo -nostdlib -noconfig -t:library -langversion:9 -warnaserror \
  -out:"$out/Studio.Pulse.Core.dll" -r:"$ns" "$repo"/unity/com.studio.pulse/Runtime/Core/*.cs
refs=()
for dll in "$engine"/UnityEngine*.dll; do refs+=("-r:$dll"); done
pkg="$repo/unity/com.studio.pulse"
for defines in "UNITY_STANDALONE_OSX" "UNITY_ANDROID" "UNITY_IOS" "UNITY_EDITOR;UNITY_ANDROID" "UNITY_WEBGL"; do
  "$mono" "$csc" -nologo -nostdlib -noconfig -t:library -langversion:9 -warnaserror \
    -define:"UNITY_2021_3_OR_NEWER;$defines" -out:"$out/Studio.Pulse.dll" \
    -r:"$ns" -r:"$out/Studio.Pulse.Core.dll" "${refs[@]}" "$pkg"/Runtime/*.cs
  echo "compiled Studio.Pulse ($defines)"
done
"$mono" "$csc" -nologo -nostdlib -noconfig -t:library -langversion:9 -warnaserror -out:"$out/QuickStart.dll" \
  -r:"$ns" -r:"$out/Studio.Pulse.Core.dll" -r:"$out/Studio.Pulse.dll" "${refs[@]}" "$pkg"/Samples~/QuickStart/*.cs
echo "compiled Samples~/QuickStart"
