#!/bin/bash
set -e

cat > /tmp/get_deps.py << 'PYEOF'
import sys, json, urllib.request, platform, os, zipfile

PYVER = f"cp{sys.version_info.major}{sys.version_info.minor}"
ARCH = platform.machine()
TARGET = "/tmp/selfheal-deps"
os.makedirs(TARGET, exist_ok=True)

def install(pkg_name):
    url = f"https://pypi.org/pypi/{pkg_name}/json"
    data = json.loads(urllib.request.urlopen(url).read())

    whl = None
    for f in data["urls"]:
        if f["packagetype"] != "bdist_wheel":
            continue
        if PYVER in f["filename"] and "manylinux" in f["filename"]:
            whl = f
            break

    if not whl:
        for f in data["urls"]:
            if f["packagetype"] != "bdist_wheel":
                continue
            if "py3-none-any" in f["filename"] or "py2.py3-none-any" in f["filename"]:
                whl = f
                break

    if not whl:
        print(f"WARN: skip {pkg_name} (no wheel found)")
        return

    path = f"/tmp/{whl['filename']}"
    print(f"Downloading {whl['filename']}...")
    urllib.request.urlretrieve(whl["url"], path)

    with zipfile.ZipFile(path, 'r') as z:
        z.extractall(TARGET)
    print(f"OK {pkg_name} -> {TARGET}")

DEPENDENCIES = [
    "PyYAML",
    "requests",
    "urllib3",
    "charset-normalizer",
    "idna",
    "certifi",
]

for pkg in DEPENDENCIES:
    install(pkg)

print("")
print("All dependencies installed.")
print(f"Run with: PYTHONPATH={TARGET}:$(pwd) python3 -m scripts.webhook_listener --host 0.0.0.0 --port 8080")
PYEOF

python3 /tmp/get_deps.py
