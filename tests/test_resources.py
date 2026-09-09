import json
import os
import shlex
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from sandbox.app import PackageRequest, Worker


@pytest.mark.parametrize("source", ["--help", "/opt/pi", "npm:foo\n--global", "./package", ""])
def test_reject_invalid_package_source(source):
    with pytest.raises(ValidationError):
        PackageRequest(action="install", source=source)


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("RUN_ISOLATION_TESTS") != "1", reason="Requires sandbox container")
async def test_native_resources_lifecycle_and_isolation(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o711)
    for parent in [tmp_path, *tmp_path.parents]:
        if parent not in (Path("/"), Path("/tmp")):
            parent.chmod(0o711)
    worker = Worker(tmp_path / "state", root)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    package = "/workspace/packages/stats-kit"

    async def names(sid):
        return {c["name"] for c in (await worker.resources(sid))["commands"]}

    async def bash(sid, command):
        result = await (await worker.connection(sid)).request("bash", command=command, timeout=30)
        assert result["exitCode"] == 0, result
        return result["output"]

    try:
        await worker.create(a)
        await worker.create(b)
        assert {"skill:python-stats", "session-info"} <= await names(a)
        assert "package-stats" not in await names(a)
        info = await worker.prompt(a, "/session-info")
        assert info["kind"] == "extension_command"
        details = json.loads(info["output"])
        assert details["uid"] == worker.uid(a)
        assert "session_info" in details["tools"]
        assert details["home"] == "/workspace/home"

        installed = await worker.package(a, PackageRequest(action="install", source=package))
        assert installed["reloaded"]
        assert {"package-stats", "skill:package-stats", "package-report"} <= await names(a)
        assert "package-stats" not in await names(b)
        assert (
            "python_stats"
            in json.loads((await worker.prompt(a, "/session-info"))["output"])["tools"]
        )
        result = await worker.prompt(a, "/package-stats 2,4,6,8")
        result_data = json.loads(result["output"])
        assert result_data["mean"] == 5 and result_data["count"] == 4
        assert result_data["python"] == "/workspace/venv/bin/python"
        # Command errors must fail promptly, rather than waiting for agent_end.
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            await worker.prompt(a, "/package-stats not-a-number")
        assert json.loads(await bash(a, "cat /workspace/package-stats.json"))["mean"] == 5
        await bash(b, "test ! -f /workspace/package-stats.json")

        # Changing a skill in A and reloading neither resets it nor modifies B.
        code = "from pathlib import Path; p=Path.home()/'.pi/agent/skills/python-stats/SKILL.md'; p.write_text(p.read_text()+'\\nSESSION_A_ONLY\\n')"
        await bash(a, "python -c " + shlex.quote(code))
        await worker.resources(a, reload=True)
        assert "SESSION_A_ONLY" in await bash(
            a, "cat /workspace/home/.pi/agent/skills/python-stats/SKILL.md"
        )
        assert "SESSION_A_ONLY" not in await bash(
            b, "cat /workspace/home/.pi/agent/skills/python-stats/SKILL.md"
        )
        assert json.loads((await worker.prompt(a, "/package-stats 10,20"))["output"])["mean"] == 15

        await worker.close()
        worker = Worker(tmp_path / "state", root)
        assert "package-stats" in await names(a)
        await worker.package(a, PackageRequest(action="remove", source=package))
        await worker.resources(a, reload=True)
        remaining = await names(a)
        assert "package-stats" not in remaining
        assert "skill:package-stats" not in remaining
        assert "package-report" not in remaining
        assert {"skill:python-stats", "session-info"} <= remaining
        assert (
            "python_stats"
            not in json.loads((await worker.prompt(a, "/session-info"))["output"])["tools"]
        )
    finally:
        await worker.close()
