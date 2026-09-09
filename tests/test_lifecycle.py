import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from orchestrator.session_manager import SessionManager
from sandbox.app import Worker


async def age(manager, seconds=100):
    timestamp = time.time() - seconds
    for row in manager.registry.db.bindings.find({}):
        manager.registry.db.bindings.update_one(
            {"_id": row["_id"]},
            {
                "$set": {
                    "last_activity": timestamp,
                    "expires_at": timestamp + row["idle_ttl_seconds"]
                    if row["retention"] == "ephemeral"
                    else None,
                }
            },
        )


def manager_at(path, handler=None):
    return SessionManager(
        path,
        {"one": "http://one"},
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                handler or (lambda request: httpx.Response(200, json={"output": "42"}))
            )
        ),
    )


@pytest.mark.asyncio
async def test_retention_dry_run_busy_agent_and_recovery(tmp_path, registry_factory):
    manager = manager_at(registry_factory(tmp_path))
    agent = (await manager.create_agent())["agent_id"]
    managed = await manager.allocate(agent, "one")
    ephemeral = await manager.allocate(agent, "one", "ephemeral", 10)
    await age(manager)
    async with manager.lock(agent):
        assert not (await manager.cleanup_once())["candidates"]
    assert (await manager.cleanup_once(dry_run=True))["candidates"] == [ephemeral["id"]]
    assert len(await manager.sessions(agent)) == 2
    await manager.close()
    manager = manager_at(registry_factory(tmp_path))
    assert (await manager.cleanup_once())["deleted"] == [ephemeral["id"]]
    assert (await manager.sessions(agent))[0]["id"] == managed["id"]
    await manager.close()


@pytest.mark.asyncio
async def test_cleanup_failure_retries_without_recreating_session(tmp_path, registry_factory):
    available = False
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "DELETE" and not available:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={})

    manager = manager_at(registry_factory(tmp_path), handler)
    agent = (await manager.create_agent())["agent_id"]
    session = await manager.allocate(agent, "one", "ephemeral", 1)
    await age(manager)
    assert (await manager.cleanup_once())["failed"][0]["status"] == 503
    assert (await manager.binding(agent, session["id"]))["status"] == "deleting"
    with pytest.raises(HTTPException):
        await manager.connect(agent, session["id"])
    available = True
    assert (await manager.cleanup_once())["deleted"] == [session["id"]]
    assert methods == ["PUT", "DELETE", "DELETE"]
    await manager.close()


@pytest.mark.asyncio
async def test_recent_worker_activity_vetoes_cleanup_and_policy_ownership(
    tmp_path, registry_factory
):
    manager = manager_at(
        registry_factory(tmp_path),
        lambda request: (
            httpx.Response(409, json={"detail": "active"})
            if request.method == "DELETE"
            else httpx.Response(200, json={})
        ),
    )
    agent = (await manager.create_agent())["agent_id"]
    other = (await manager.create_agent())["agent_id"]
    session = await manager.allocate(agent, "one", "ephemeral", 10)
    await age(manager)
    with pytest.raises(HTTPException):
        await manager.retention(other, session["id"], "managed")
    assert (await manager.cleanup_once())["failed"][0]["status"] == 409
    assert (await manager.binding(agent, session["id"]))["status"] == "ready"
    assert not (await manager.cleanup_once())["candidates"]
    await manager.retention(agent, session["id"], "managed")
    await age(manager)
    assert not (await manager.cleanup_once())["candidates"]
    await manager.close()


@pytest.mark.asyncio
async def test_worker_suspend_preserves_files_and_delete_checks_activity(
    tmp_path, registry_factory, monkeypatch
):
    monkeypatch.setenv("PI_IDLE_DISCONNECT_SECONDS", "10")
    worker = Worker(tmp_path, tmp_path / "sessions")
    worker.db.execute("INSERT INTO sessions(id, last_activity) VALUES ('s', 1)")
    directory = worker.root / "s"
    directory.mkdir(parents=True)
    (directory / "evidence.py").write_text("print(42)")
    closed = []

    async def close():
        closed.append(True)

    worker.connections["s"] = SimpleNamespace(close=close)
    async with worker.activity("s"):
        assert await worker.suspend_idle() == []
        with pytest.raises(HTTPException) as exc:
            await worker.delete("s", idle_before=time.time())
        assert exc.value.status_code == 409
    with pytest.raises(HTTPException):
        await worker.delete("s", idle_before=time.time() - 10)
    worker.db.execute("UPDATE sessions SET last_activity=1")
    assert await worker.suspend_idle() == ["s"]
    assert closed and (directory / "evidence.py").exists()
    assert worker.uid("s") == 10001
    await worker.delete("s", idle_before=time.time())
    await worker.delete("s", idle_before=time.time())  # Idempotent retry.
    assert not directory.exists()
    await worker.close()


@pytest.mark.asyncio
async def test_worker_reuses_released_uid_and_background_shutdown(
    tmp_path, registry_factory, monkeypatch
):
    worker = Worker(tmp_path, tmp_path / "sessions")
    monkeypatch.setenv("MAX_SESSIONS", "1")
    monkeypatch.setattr("sandbox.app.provision", lambda path, uid: path.mkdir(parents=True))

    async def connection(sid):
        return None

    monkeypatch.setattr(worker, "connection", connection)
    await worker.create("a")
    uid = worker.uid("a")
    await worker.delete("a")
    await worker.create("b")
    assert worker.uid("b") == uid
    worker.start_cleanup()
    task = worker.cleanup_task
    await worker.close()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_background_sweep_and_successful_prompt_refresh_ttl(tmp_path, registry_factory):
    manager = manager_at(registry_factory(tmp_path))
    agent = (await manager.create_agent())["agent_id"]
    session = await manager.allocate(agent, "one", "ephemeral", 10)
    await age(manager)
    await manager.prompt(agent, session["id"], "hello")
    assert not (await manager.cleanup_once())["candidates"]
    await age(manager)
    manager.cleanup_task = asyncio.create_task(manager.cleanup_loop(0.01))
    async with asyncio.timeout(2):
        while await manager.sessions(agent):
            await asyncio.sleep(0.01)
    await manager.close()


@pytest.mark.asyncio
async def test_real_pi_suspend_reconnect_and_expire(tmp_path, registry_factory, monkeypatch):
    import os
    import uuid
    from pathlib import Path

    if os.getenv("RUN_ISOLATION_TESTS") != "1":
        pytest.skip("Requires Linux sandbox image")
    monkeypatch.setenv("PI_IDLE_DISCONNECT_SECONDS", "10")
    for parent in [tmp_path, *tmp_path.parents]:
        if parent not in (Path("/"), Path("/tmp")):
            parent.chmod(0o711)
    root = tmp_path / "sessions"
    root.mkdir(mode=0o711)
    worker = Worker(tmp_path / "state", root)
    sid = str(uuid.uuid4())
    try:
        await worker.create(sid)
        async with worker.activity(sid):
            rpc = await worker.connection(sid)
            result = await rpc.request(
                "bash", command="printf 'print(6 * 7)\\n' > result.py; python result.py", timeout=30
            )
            assert result["exitCode"] == 0 and "42" in result["output"]
        assert await worker.suspend_idle(now=time.time() + 20) == [sid]
        assert rpc.process.returncode is not None
        assert (root / sid / "data" / "result.py").exists()
        await worker.close()
        worker = Worker(tmp_path / "state", root)
        await worker.create(sid)
        async with worker.activity(sid):
            restored = await worker.connection(sid)
            result = await restored.request("bash", command="python result.py", timeout=30)
            assert result["exitCode"] == 0 and "42" in result["output"]
        await worker.delete(sid, idle_before=time.time() + 1)
        assert restored.process.returncode is not None
        assert not (root / sid).exists()
        assert worker.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_concurrent_sweeps_tolerate_deleted_snapshot_rows(tmp_path, registry_factory):
    first_delete = asyncio.Event()
    release = asyncio.Event()
    first = None

    async def handler(request):
        if request.method == "DELETE" and request.url.path.endswith(first):
            first_delete.set()
            await release.wait()
        return httpx.Response(200, json={})

    manager = manager_at(registry_factory(tmp_path), handler)
    a = (await manager.create_agent())["agent_id"]
    b = (await manager.create_agent())["agent_id"]
    first = (await manager.allocate(a, "one", "ephemeral", 1))["id"]
    second = (await manager.allocate(b, "one", "ephemeral", 1))["id"]
    await age(manager)
    sweep = asyncio.create_task(manager.cleanup_once())
    await first_delete.wait()
    assert (await manager.cleanup_once())["deleted"] == [second]
    release.set()
    assert (await sweep)["deleted"] == [first]
    assert (await manager.sessions(a)) == (await manager.sessions(b)) == []
    await manager.close()
