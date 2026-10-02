"""
Refreshes the --hash values of KitsuUnreal.uplugin's PythonRequirements from PyPI:

    python scripts/update_pip_hashes.py

Unreal installs plugin requirements with `pip --require-hashes` when the project's
bPipStrictHashCheck is on (the default), and pip then wants every package it installs,
dependencies included, pinned with `==` and hashed. To add or bump a package, edit its
`name==version` in the .uplugin (keep the dependencies listed and pinned too) and run this.

Hashes are those of the wheels CPython 3.11 (UE 5.7's Python) can install, on every platform
for the "All" entry and on Windows only for "Win64". Versions shared with engine plugins
(PythonFoundationPackages: requests, urllib3, typing-extensions...) must match their pins.
"""
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLUGIN = os.path.join(ROOT, "UnrealEngine5", "UnrealEnginePlugins", "KitsuUnreal", "KitsuUnreal.uplugin")
PLATFORM_TAGS = {"Win64": "win_amd64"}


def usable_by_cp311(filename):
    # <name>-<version>(-<build>)?-<python>-<abi>-<platform>.whl
    python, abi, _ = filename[:-4].split("-")[-3:]
    tags = python.split(".")
    if abi in ("none", "cp311", "abi3") and any(t in ("py3", "py2.py3", "cp311") for t in tags):
        return True
    return abi == "abi3" and any(re.fullmatch(r"cp3\d+", t) and int(t[3:]) <= 11 for t in tags)


def hashed_line(line, platform):
    match = re.match(r"\s*([A-Za-z0-9_.\-]+)==([^\s;]+)", line)
    if not match:
        sys.exit(f"not pinned with ==: {line!r}")
    name, version = match.groups()
    data = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json"))
    wheels = [f for f in data["urls"] if f["packagetype"] == "bdist_wheel" and usable_by_cp311(f["filename"])]
    tag = PLATFORM_TAGS.get(platform)
    if tag:
        wheels = [f for f in wheels if tag in f["filename"] or f["filename"].endswith("-any.whl")]
    if not wheels:
        sys.exit(f"no wheel for CPython 3.11 ({platform}) in {name}=={version}")
    hashes = sorted({f["digests"]["sha256"] for f in wheels})
    print(f"{platform:5} {name}=={version}: {len(hashes)} wheel(s)")
    return f"{name}=={version} " + " ".join(f"--hash=sha256:{h}" for h in hashes)


raw = open(UPLUGIN, encoding="utf-8", newline="").read()
newline = "\r\n" if "\r\n" in raw else "\n"
plugin = json.loads(raw)
for entry in plugin["PythonRequirements"]:
    entry["Requirements"] = [hashed_line(line, entry["Platform"]) for line in entry["Requirements"]]
with open(UPLUGIN, "w", encoding="utf-8", newline="") as f:
    f.write(json.dumps(plugin, indent="\t", ensure_ascii=False).replace("\n", newline) + newline)
print(f"updated {UPLUGIN}")
