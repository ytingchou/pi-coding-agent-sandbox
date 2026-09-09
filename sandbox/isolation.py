"""All generated code runs after UID drop and inside a private mount/PID namespace."""
import os
import resource
from pathlib import Path


def provision(root: Path, uid: int) -> None:
    # The parent is root-owned so a session cannot replace its bind-mount source.
    root.mkdir(mode=0o711)
    data = root / "data"
    data.mkdir(mode=0o700)
    os.chown(data, uid, uid)


def command(root: Path, argv: list[str]) -> list[str]:
    args = [
        "/usr/bin/bwrap", "--die-with-parent", "--new-session",
        "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--cap-drop", "ALL", "--hostname", "pi-session",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/opt/pi", "/opt/pi",
        "--ro-bind", "/opt/pi-resources", "/opt/pi-resources",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
        "--symlink", "usr/lib", "/lib",
    ]
    if Path("/lib64").exists():
        args += ["--symlink", "usr/lib64", "/lib64"]
    for path in ("/etc/ssl", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
        args += ["--ro-bind", path, path]
    args += [
        "--bind", str(root / "data"), "/workspace",
        "--tmpfs", "/tmp", "--tmpfs", "/run", "--proc", "/proc", "--dev", "/dev",
        "--chdir", "/workspace", "--", *argv,
    ]
    return args


def environment() -> dict[str, str]:
    # Explicit allowlist: never pass the worker API token or supervisor settings.
    return {
        "PATH": "/workspace/venv/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/workspace/home", "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": "/workspace/home/.cache",
        "npm_config_cache": "/workspace/home/.cache/npm",
        "npm_config_prefix": "/workspace/home/.local",
        "GIT_TERMINAL_PROMPT": "0",
        "PI_CODING_AGENT_DIR": "/workspace/home/.pi/agent",
        "VIRTUAL_ENV": "/workspace/venv", "PYTHONNOUSERSITE": "1",
        "LANG": "C.UTF-8", "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
    }


def limits() -> None:
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024,) * 2)


# Run venv bootstrap inside isolation as the session UID, never as supervisor root.
BOOTSTRAP = """
set -eu
mkdir -p /workspace/home /workspace/state
if [ ! -x /workspace/venv/bin/python ]; then
    /usr/bin/python3 -m venv /workspace/venv
fi
/usr/bin/python3 /opt/pi-resources/bootstrap.py
exec pi --mode rpc --provider openai --model "$1" \
    --session /workspace/state/session.jsonl \
    --append-system-prompt 'For coding tasks, write a Python file in /workspace and execute it with python. Use the session venv. Report actual stdout and errors; never invent execution results.'
"""
