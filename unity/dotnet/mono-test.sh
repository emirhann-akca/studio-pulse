#!/usr/bin/env bash
# Builds and tests the Unity SDK core without the .NET SDK, with the Mono that ships
# inside a Unity Editor: the core against the netstandard 2.1 reference assembly
# (warnings are errors), the tests and the E2E driver against Mono's own framework.
#   unity/dotnet/mono-test.sh [test name filter]
# UNITY_CONTENTS=/Applications/Unity/Hub/Editor/<version>/Unity.app/Contents overrides the lookup.
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
facade="$mono_dir/lib/mono/4.5/Facades/netstandard.dll"
out="${PULSE_MONO_OUT:-$repo/unity/dotnet/bin-mono}"
mkdir -p "$out"
"$mono" "$csc" -nologo -nostdlib -noconfig -t:library -langversion:9 -warnaserror -optimize+ \
  -out:"$out/Studio.Pulse.Core.dll" -r:"$ns" "$repo"/unity/com.studio.pulse/Runtime/Core/*.cs
"$mono" "$csc" -nologo -langversion:9 -warnaserror -out:"$out/Studio.Pulse.Core.Tests.exe" \
  -r:"$out/Studio.Pulse.Core.dll" -r:"$facade" -r:System.Net.Http.dll -r:System.IO.Compression.dll \
  "$here"/Studio.Pulse.Core.Tests/{TestKit,CoreTests,ConformanceTests,AssignTests,E2EScenario,MonoMain}.cs
"$mono" "$csc" -nologo -langversion:9 -warnaserror -out:"$out/Studio.Pulse.Driver.exe" \
  -r:"$out/Studio.Pulse.Core.dll" -r:"$facade" -r:System.Net.Http.dll -r:System.IO.Compression.dll \
  -main:Studio.Pulse.Driver.Program \
  "$here"/Studio.Pulse.Driver/Program.cs "$here"/Studio.Pulse.Core.Tests/{TestKit,E2EScenario}.cs
cd "$out"
# GZipStream on Mono needs the native zlib helper that ships next to the Editor
export DYLD_LIBRARY_PATH="$UNITY_CONTENTS/Frameworks${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
PULSE_REPO="$repo" "$mono" --debug "$out/Studio.Pulse.Core.Tests.exe" "$@"
