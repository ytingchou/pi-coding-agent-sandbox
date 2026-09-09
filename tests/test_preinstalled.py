"""Run under network_mode:none to prove build-time libraries work in real Pi sessions."""

import json
import os
import shlex
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_ISOLATION_TESTS") != "1", reason="Requires sandbox image"
)


@pytest.mark.asyncio
async def test_image_packages_inventory_and_read_only_sharing(tmp_path):
    from sandbox.app import Worker

    root = tmp_path / "sessions"
    root.mkdir(mode=0o711)
    for parent in [tmp_path, *tmp_path.parents]:
        if parent not in (Path("/"), Path("/tmp")):
            parent.chmod(0o711)
    worker = Worker(tmp_path / "state", root)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())

    async def bash(sid, text):
        result = await (await worker.connection(sid)).request("bash", command=text, timeout=30)
        if result["exitCode"] != 0:
            raise AssertionError(result["output"])
        return result["output"]

    check = """import json, os, sys
from pathlib import Path
from importlib import metadata
import numpy as np
import rich
import requests
from minio import Minio
import sqlite3
assert sys.version_info[:2] == (3, 12)
assert sys.prefix == '/workspace/venv'
assert np.arange(1, 6).dot(np.arange(1, 6)) == 55
assert str(Path(np.__file__)).startswith('/usr/local/lib/python3.12/site-packages/')
assert not any('/opt/server' in p for p in sys.path)
assert os.environ['PIP_NO_INDEX'] == '1'
assert os.environ['UV_OFFLINE'] == 'true'
image=json.loads(Path('/opt/python-runtime/packages.json').read_text())
effective=json.loads(Path('/workspace/state/python-packages.json').read_text())
assert requests.Request('GET','http://internal.invalid').prepare().method == 'GET'
assert Minio('minio.internal:9000', access_key='example', secret_key='example', secure=False) is not None
with sqlite3.connect('/workspace/demo.sqlite') as db:
 assert db.execute('SELECT 6 * 7').fetchone()[0] == 42
assert image['standard_library']['sqlite3']['sqlite_version'] == sqlite3.sqlite_version
for name in ['numpy','rich','requests','minio']:
 pkg=next(p for p in image['packages'] if p['name']==name)
 assert pkg['version']==metadata.version(name)
 assert name in pkg['imports']
 assert next(p for p in effective['packages'] if p['name']==name)['version']==pkg['version']
# Pi can rewrite its process title; verify the prompt file here and actual HTTP
# system-message injection in test_gateway.py instead of relying on /proc argv.
prompt=Path('/opt/python-runtime/SYSTEM.md').read_text().strip()
assert 'Do not run pip install' in prompt
assert 'requests==' in prompt and 'minio==' in prompt and 'sqlite3' in prompt
for target in [Path(np.__file__), Path('/opt/python-runtime/packages.json')]:
 try:
  with target.open('r+'):
   pass
 except OSError:
  pass
 else:
  raise AssertionError('Image package and manifest must be read-only')
print(json.dumps({'sum_squares':55,'numpy':np.__version__,'rich':metadata.version('rich')}))
"""
    try:
        for sid in (a, b):
            await worker.create(sid)
            # Write then execute actual Python, with no pip/uv install or model calls.
            await bash(
                sid,
                "python -c "
                + shlex.quote(
                    "from pathlib import Path; Path('preinstalled_demo.py').write_text("
                    + repr(check)
                    + ")"
                ),
            )
            result = json.loads(await bash(sid, "python preinstalled_demo.py"))
            assert result["sum_squares"] == 55
        # Old 3.12 venvs gain base-package visibility when reconnected, with files intact.
        await bash(
            a,
            "python -c "
            + shlex.quote(
                "from pathlib import Path; p=Path('/workspace/venv/pyvenv.cfg'); p.write_text(p.read_text().replace('include-system-site-packages = true','include-system-site-packages = false'))"
            ),
        )
        await worker.disconnect(a)
        assert json.loads(await bash(a, "python preinstalled_demo.py"))["sum_squares"] == 55
    finally:
        await worker.close()
