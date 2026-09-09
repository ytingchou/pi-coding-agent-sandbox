"""Build-time package manifest and session-visible effective inventory (no network)."""

import argparse
import hashlib
import json
import platform
import re
import sqlite3
import sys
from importlib import metadata
from pathlib import Path


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def inventory():
    imports = {}
    for module, names in metadata.packages_distributions().items():
        for name in names:
            imports.setdefault(normalized(name), []).append(module)
    # First occurrence follows Python's metadata search precedence (venv before base).
    found = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if name and normalized(name) not in found:
            found[normalized(name)] = {
                "name": name,
                "version": dist.version,
                "imports": sorted(imports.get(normalized(name), [])),
                "location": str(dist.locate_file("")),
            }
    return {
        "python": platform.python_version(),
        "executable": sys.executable,
        "standard_library": {"sqlite3": {"sqlite_version": sqlite3.sqlite_version}},
        "packages": sorted(found.values(), key=lambda p: normalized(p["name"])),
    }


def build(directory):
    directory.mkdir(parents=True, exist_ok=True)
    data = inventory()
    data["requirements_sha256"] = hashlib.sha256(
        (directory / "requirements.txt").read_bytes()
    ).hexdigest()
    data["policy"] = "preinstalled-only"
    (directory / "packages.json").write_text(json.dumps(data, indent=2) + "\n")
    packages = "; ".join(f"{p['name']}=={p['version']}" for p in data["packages"])
    prompt = (
        "For coding tasks, write a Python file in /workspace and execute it with python. "
        "Use the Python 3.12 session venv. Report actual stdout and errors; never invent results.\n"
        "This deployment has no external package network access. Python dependencies are installed "
        "ONLY during image build. Do not run pip install, uv pip install, uv add/sync, "
        "or download dependencies at runtime. Use the existing libraries or Python standard library. "
        "If a required dependency is missing, report its name and ask the operator to add it to "
        "the sandbox dependency group in pyproject.toml, run uv lock, and rebuild the image. Do not retry installation.\n"
        f"Image preinstalled distributions (including transitive dependencies): {packages}\n"
        f"Python standard library sqlite3 is available (SQLite {sqlite3.sqlite_version}); do not pip install sqlite3.\n"
        "Full image inventory with import-name hints: /opt/python-runtime/packages.json. "
        "Effective session inventory: /workspace/state/python-packages.json. "
        "Read the inventory or check imports when needed; distribution names can differ from import names. "
        "Old sessions may contain local packages that override image versions.\n"
    )
    (directory / "SYSTEM.md").write_text(prompt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path)
    args = parser.parse_args()
    if args.build:
        build(args.build)
    else:
        print(json.dumps(inventory(), indent=2))
