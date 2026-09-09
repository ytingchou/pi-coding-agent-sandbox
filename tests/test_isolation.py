"""Linux container integration: real pi RPC bash commands, no model/API key needed."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_ISOLATION_TESTS") != "1", reason="Run inside the sandbox test container")


@pytest.mark.asyncio
async def test_real_pi_sessions_are_isolated_and_reconnect(tmp_path):
    from sandbox.app import Worker
    root = tmp_path / "sessions"
    root.mkdir(mode=0o711)
    # pytest ancestors default to 0700; distinct session UIDs need traversal.
    for parent in [tmp_path, *tmp_path.parents]:
        if parent != Path("/") and parent != Path("/tmp"):
            parent.chmod(0o711)
    worker = Worker(tmp_path / "state", root)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        await worker.create(a)
        await worker.create(b)
        assert worker.uid(a) != worker.uid(b)
        async def bash(sid, command):
            rpc = await worker.connection(sid)
            result = await rpc.request("bash", timeout=30, command=command)
            assert result["exitCode"] == 0, result
            return result["output"]
        code = "from pathlib import Path; Path('result.py').write_text('print(6 * 7)\\n'); Path('/tmp/only_a').write_text('private'); print('written')"
        assert "written" in await bash(a, "python -c " + shlex.quote(code))
        assert "42" in await bash(a, "python result.py")
        # Install a tiny real local distribution with pip into A's venv, offline.
        install = """import pathlib, zipfile
p=pathlib.Path('/tmp/session_only-1.0-py3-none-any.whl')
with zipfile.ZipFile(p,'w') as z:
 z.writestr('session_only.py','VALUE = 42\\n')
 z.writestr('session_only-1.0.dist-info/METADATA','Metadata-Version: 2.1\\nName: session_only\\nVersion: 1.0\\n')
 z.writestr('session_only-1.0.dist-info/WHEEL','Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')
 z.writestr('session_only-1.0.dist-info/RECORD','')
"""
        await bash(a, "python -c " + shlex.quote(install) + " && python -m pip install --no-index /tmp/session_only-1.0-py3-none-any.whl && python -c 'import session_only; assert session_only.VALUE == 42'")
        inspect = """import os, pathlib, importlib.util, json
assert not pathlib.Path('/workspace/result.py').exists()
assert not pathlib.Path('/tmp/only_a').exists()
assert not pathlib.Path('/sessions').exists()
assert not pathlib.Path('/app').exists()
assert not pathlib.Path('/state').exists()
assert not pathlib.Path('/opt/server').exists()
assert importlib.util.find_spec('session_only') is None
assert 'SANDBOX_TOKEN' not in os.environ
assert 'API_TOKEN' not in os.environ
assert os.getuid() != 0
assert int(pathlib.Path('/proc/self/status').read_text().split('CapEff:')[1].splitlines()[0].strip(),16) == 0
assert not any('/opt/server/bin/uvicorn' in p.read_text(errors='ignore').split(chr(0)) for p in pathlib.Path('/proc').glob('[0-9]*/cmdline'))
try:
 pathlib.Path('/usr/should-fail').write_text('x')
except OSError:
 pass
else:
 raise AssertionError('System filesystem must be read-only')
print(json.dumps({'isolated': True, 'uid': os.getuid(), 'python': os.sys.executable}))
"""
        assert json.loads((await bash(b, "python -c " + shlex.quote(inspect))).strip())["isolated"]
        # Distinct sessions execute concurrently, each RPC stream stays correlated.
        results = await asyncio.gather(bash(a, "python -c 'print(111)'"), bash(b, "python -c 'print(222)'"))
        assert "111" in results[0] and "222" in results[1]
        await worker.close()
        worker = Worker(tmp_path / "state", root)
        assert "42" in await bash(a, "python result.py && python -c 'import session_only; print(session_only.VALUE)'")
        history = await (await worker.connection(a)).request("get_messages")
        assert "result.py" in json.dumps(history)
        await worker.delete(a)
        assert not (root / a).exists()
        assert "still-alive" in await bash(b, "echo still-alive")
    finally:
        await worker.close()
