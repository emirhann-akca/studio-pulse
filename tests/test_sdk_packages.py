"""The Unity and Flutter SDK packages stay consistent with each other and the contract.

Static checks that need no Unity, .NET or Flutter: versions, the UnityEngine-free
core, native symbol names on both sides of the bridges, the privacy manifests the
generator copies into both packages, and the Unity .meta files.
"""

from __future__ import annotations

import importlib.util
import json
import plistlib
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
UNITY = ROOT / "unity" / "com.studio.pulse"
FLUTTER = ROOT / "flutter" / "studio_pulse"


def test_versions_agree():
    pkg = json.loads((UNITY / "package.json").read_text(encoding="utf-8"))
    cs = (UNITY / "Runtime/Core/PulseCore.cs").read_text(encoding="utf-8")
    dart = (FLUTTER / "lib/src/core/core.dart").read_text(encoding="utf-8")
    pubspec = (FLUTTER / "pubspec.yaml").read_text(encoding="utf-8")
    cs_version = re.search(r'public const string Version = "([^"]+)";', cs).group(1)
    dart_version = re.search(r"static const String version = '([^']+)';", dart).group(1)
    pub_version = re.search(r"^version: (\S+)$", pubspec, flags=re.M).group(1)
    assert pkg["version"] == cs_version == dart_version == pub_version == "2.0.0"
    assert 'SdkName = "unity/" + Version' in cs and "sdkName = 'flutter/$version'" in dart


def test_cores_are_free_of_engine_code():
    for f in (UNITY / "Runtime/Core").glob("*.cs"):
        code = "\n".join(line.split("//")[0] for line in f.read_text(encoding="utf-8").splitlines())
        assert "using UnityEngine" not in code and "UnityEngine." not in code, f.name
    asmdef = json.loads(
        (UNITY / "Runtime/Core/Studio.Pulse.Core.asmdef").read_text(encoding="utf-8")
    )
    assert asmdef["noEngineReferences"] is True and asmdef["name"] == "Studio.Pulse.Core"
    adapter = json.loads((UNITY / "Runtime/Studio.Pulse.asmdef").read_text(encoding="utf-8"))
    assert adapter["references"] == ["Studio.Pulse.Core"]
    for f in (FLUTTER / "lib/src/core").glob("*.dart"):
        text = f.read_text(encoding="utf-8")
        assert "package:flutter" not in text and "dart:ui" not in text, f.name


def test_native_bridges_use_the_same_names():
    swift = (UNITY / "Plugins/iOS/StudioPulseInstallSource.swift").read_text(encoding="utf-8")
    cs = (UNITY / "Runtime/UnityPulsePlatform.cs").read_text(encoding="utf-8")
    exported = set(re.findall(r'@_cdecl\("(\w+)"\)', swift))
    imported = set(re.findall(r"static extern \w+ (\w+)\(", cs))
    assert imported and imported <= exported, imported - exported
    kotlin = next((FLUTTER / "android").rglob("StudioPulsePlugin.kt")).read_text(encoding="utf-8")
    ios = (FLUTTER / "ios/Classes/StudioPulsePlugin.swift").read_text(encoding="utf-8")
    facade = (FLUTTER / "lib/src/flutter/studio_pulse.dart").read_text(encoding="utf-8")
    for side in (kotlin, ios, facade):
        assert '"studio_pulse"' in side or "'studio_pulse'" in side
        assert "info" in side and "excludeFromBackup" in side
    # every key the facade reads is answered by at least one platform
    for key in re.findall(r"info\['(\w+)'\]", facade):
        assert f'"{key}"' in kotlin or f'"{key}"' in ios, key
    pubspec = (FLUTTER / "pubspec.yaml").read_text(encoding="utf-8")
    assert "pluginClass: StudioPulsePlugin" in pubspec
    assert "resource_bundles" in (FLUTTER / "ios/studio_pulse.podspec").read_text(encoding="utf-8")


def test_privacy_manifests_ship_with_both_sdks():
    generated = (ROOT / "contract/generated/PrivacyInfo.xcprivacy").read_bytes()
    for copy in (
        UNITY / "Plugins/iOS/PrivacyInfo.xcprivacy",
        FLUTTER / "ios/Resources/PrivacyInfo.xcprivacy",
    ):
        assert copy.read_bytes() == generated, copy
    m = plistlib.loads(generated)
    api = {
        a["NSPrivacyAccessedAPIType"]: a["NSPrivacyAccessedAPITypeReasons"]
        for a in m["NSPrivacyAccessedAPITypes"]
    }
    assert api == {"NSPrivacyAccessedAPICategoryFileTimestamp": ["C617.1"]}
    meta = (UNITY / "Plugins/iOS/PrivacyInfo.xcprivacy.meta").read_text(encoding="utf-8")
    assert "PluginImporter" in meta and "iPhone: iOS" in meta and "enabled: 1" in meta


def test_unity_meta_files_are_complete():
    spec = importlib.util.spec_from_file_location("unity_meta", ROOT / "unity/tools/unity_meta.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    want = mod.wanted()
    assert len(want) >= 30
    for meta, text in want.items():
        assert meta.exists() and meta.read_text(encoding="utf-8") == text, meta
    guids = [re.search(r"guid: (\w+)", t).group(1) for t in want.values()]
    assert len(set(guids)) == len(guids)
    assert not list(UNITY.glob("Samples~/**/*.meta"))  # Unity ignores folders ending in ~


def test_sdk_docs_cover_the_api():
    unity = (ROOT / "docs/SDK_UNITY.md").read_text(encoding="utf-8")
    flutter = (ROOT / "docs/SDK_FLUTTER.md").read_text(encoding="utf-8")
    facade = (UNITY / "Runtime/PulseSdk.cs").read_text(encoding="utf-8")
    for name in set(re.findall(r"public static \w+(?:<\w+>)? (\w+)\(", facade)) - {"Init"}:
        assert f"`{name}(" in unity or f"{name}(" in unity, name
    dart = (FLUTTER / "lib/src/flutter/studio_pulse.dart").read_text(encoding="utf-8")
    for name in re.findall(r"^  static (?:void|bool|Future<void>) (\w+)\(", dart, flags=re.M):
        if name.startswith("_") or name == "shutdown":
            continue
        assert f"{name}(" in flutter or f"`{name}`" in flutter or name in flutter, name
    for doc in (unity, flutter):
        assert "Not verified on a device" in doc
