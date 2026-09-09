"""Seed examples once, as the session UID inside Bubblewrap. Never overwrite edits."""
import json
import shutil
from pathlib import Path


def seed():
    source = Path("/opt/pi-resources")
    agent_dir = Path("/workspace/home/.pi/agent")
    agent_dir.mkdir(parents=True, exist_ok=True)
    marker = agent_dir / ".sample-resources-v1"
    if marker.exists():
        return
    for directory in ("skills", "extensions"):
        target = agent_dir / directory
        target.mkdir(exist_ok=True)
        for item in (source / directory).iterdir():
            destination = target / item.name
            if not destination.exists():
                if item.is_dir():
                    shutil.copytree(item, destination)
                else:
                    shutil.copyfile(item, destination)
    packages = Path("/workspace/packages")
    packages.mkdir(exist_ok=True)
    for item in (source / "packages").iterdir():
        if not (packages / item.name).exists():
            if item.is_dir():
                shutil.copytree(item, packages / item.name)
            else:
                shutil.copyfile(item, packages / item.name)
    settings = agent_dir / "settings.json"
    if not settings.exists():
        settings.write_text(json.dumps({"enableSkillCommands": True}) + "\n")
    # The package is copied but not installed: demos exercise native `pi install`.
    marker.write_text("1\n")


if __name__ == "__main__":
    seed()
