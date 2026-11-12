"""Writes (or checks) the .meta files of the Unity package.

Unity ignores files without a .meta in an immutable package (one added by git
URL), so every file and folder of unity/com.studio.pulse has one, with a GUID
derived from its path (stable across machines). Native plug-in sources and the
privacy manifest get a PluginImporter enabled for iOS only.

    python unity/tools/unity_meta.py          # write missing / outdated .meta files
    python unity/tools/unity_meta.py --check  # exit 1 if any is missing or differs
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "com.studio.pulse"

TAIL = "  userData: \n  assetBundleName: \n  assetBundleVariant: \n"

IOS_PLUGIN = (
    """PluginImporter:
  externalObjects: {}
  serializedVersion: 2
  iconMap: {}
  executionOrder: {}
  defineConstraints: []
  isPreloaded: 0
  isOverridable: 1
  isExplicitlyReferenced: 0
  validateReferences: 1
  platformData:
  - first:
      Any: 
    second:
      enabled: 0
      settings: {}
  - first:
      Editor: Editor
    second:
      enabled: 0
      settings:
        DefaultValueInitialized: true
  - first:
      iPhone: iOS
    second:
      enabled: 1
      settings: {}
"""
    + TAIL
)


def guid(rel: str) -> str:
    return hashlib.md5(("com.studio.pulse/" + rel).encode("utf-8")).hexdigest()


def meta_for(path: Path) -> str:
    rel = path.relative_to(PKG).as_posix()
    head = f"fileFormatVersion: 2\nguid: {guid(rel)}\n"
    if path.is_dir():
        return head + "folderAsset: yes\nDefaultImporter:\n  externalObjects: {}\n" + TAIL
    ext = path.suffix.lower()
    if ext == ".cs":
        return head + (
            "MonoImporter:\n  externalObjects: {}\n  serializedVersion: 2\n  defaultReferences: []\n"
            "  executionOrder: 0\n  icon: {instanceID: 0}\n" + TAIL
        )
    if ext == ".asmdef":
        return head + "AssemblyDefinitionImporter:\n  externalObjects: {}\n" + TAIL
    if path.name == "package.json":
        return head + "PackageManifestImporter:\n  externalObjects: {}\n" + TAIL
    if ext in (".swift", ".mm", ".m", ".xcprivacy") and "Plugins/iOS" in rel:
        return head + IOS_PLUGIN
    if ext in (".md", ".txt", ".json"):
        return head + "TextScriptImporter:\n  externalObjects: {}\n" + TAIL
    return head + "DefaultImporter:\n  externalObjects: {}\n" + TAIL


def wanted() -> dict[Path, str]:
    out = {}
    for p in sorted(PKG.rglob("*")):
        rel = p.relative_to(PKG)
        if p.suffix == ".meta" or any(
            part.endswith("~") or part.startswith(".") for part in rel.parts
        ):
            continue
        out[p.with_name(p.name + ".meta")] = meta_for(p)
    return out


def main(argv: list[str]) -> int:
    check = "--check" in argv
    bad = []
    want = wanted()
    for meta, text in want.items():
        if not meta.exists() or meta.read_text(encoding="utf-8") != text:
            bad.append(meta)
            if not check:
                meta.write_text(text, encoding="utf-8", newline="\n")
    stray = [m for m in PKG.rglob("*.meta") if m not in want]
    for m in stray:
        bad.append(m)
        if not check:
            m.unlink()
    for m in bad:
        print(("out of date: " if check else "wrote: ") + m.relative_to(PKG.parent).as_posix())
    return 1 if (check and bad) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
