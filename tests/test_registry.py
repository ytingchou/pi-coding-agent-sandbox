import asyncio

import pytest
from fastapi import HTTPException
from pymongo.errors import ConnectionFailure

from orchestrator.registry import MongoRegistry
from orchestrator.session_manager import SessionManager


@pytest.mark.asyncio
async def test_indexes_uniqueness_and_durable_registry(tmp_path, registry_factory):
    registry = registry_factory(tmp_path)
    await registry.initialize()
    manager = SessionManager(registry, endpoints={"one": "http://one"})
    agent = (await manager.create_agent())["agent_id"]
    await registry.insert(
        "sid",
        agent_id=agent,
        sandbox_id="one",
        status="deleting",
        retention="ephemeral",
        idle_ttl_seconds=10,
        last_activity=1,
        created_at=1,
        expires_at=11,
    )
    with pytest.raises(HTTPException) as exc:
        await registry.insert(
            "sid",
            agent_id="intruder",
            sandbox_id="one",
            status="ready",
            retention="managed",
            idle_ttl_seconds=0,
            last_activity=1,
            created_at=1,
            expires_at=None,
        )
    assert exc.value.status_code == 503
    await manager.close()
    restored = SessionManager(registry_factory(tmp_path), endpoints={"one": "http://one"})
    assert (await restored.sessions(agent))[0]["status"] == "deleting"
    assert len(await restored.registry.expired(12)) == 1
    assert all("expireAfterSeconds" not in item for item in registry.db.bindings.list_indexes())
    assert (await registry.binding("sid", "intruder")) is None
    await restored.close()


@pytest.mark.asyncio
async def test_database_outage_is_redacted_and_does_not_block_loop(
    tmp_path, registry_factory, monkeypatch
):
    registry = registry_factory(tmp_path)

    def fail(*args, **kwargs):
        raise ConnectionFailure("mongodb://private-user:private-password@internal-host")

    monkeypatch.setattr(type(registry.db.agents), "find_one", fail)
    with pytest.raises(HTTPException) as exc:
        await registry.get_agent("a")
    assert exc.value.detail == "MongoDB registry unavailable"
    assert "private" not in str(exc.value)
    await asyncio.sleep(0)


def test_missing_or_invalid_mongo_configuration_is_safe(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    with pytest.raises(ValueError, match="MONGODB_URI is required"):
        MongoRegistry()
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("MONGODB_TIMEOUT_MS", "0")
    with pytest.raises(ValueError, match="positive integer"):
        MongoRegistry()
    monkeypatch.setenv("MONGODB_TIMEOUT_MS", "5000")
    monkeypatch.setenv("MONGODB_URI", "invalid://private-user:private-password@host")
    monkeypatch.delenv("MONGODB_USERNAME", raising=False)
    monkeypatch.delenv("MONGODB_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="Invalid MongoDB connection configuration"):
        MongoRegistry()


@pytest.mark.asyncio
async def test_cancelled_database_write_finishes_before_releasing_caller_lock():
    import threading

    from orchestrator.registry import database_operation

    started, release = threading.Event(), threading.Event()
    completed = []

    @database_operation
    def write():
        started.set()
        assert release.wait(2)
        completed.append(True)

    task = asyncio.create_task(write())
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert completed == [True]


@pytest.mark.asyncio
async def test_manager_starts_with_only_mongodb(tmp_path, registry_factory, monkeypatch):
    monkeypatch.setenv("SESSION_CLEANUP_INTERVAL_SECONDS", "0")
    registry = registry_factory(tmp_path)
    manager = SessionManager(registry, endpoints={"one": "http://one"})
    try:
        await manager.start()
        agent = (await manager.create_agent())["agent_id"]
        assert await manager.sessions(agent) == []
        assert set(registry.db.list_collection_names()) == {"agents", "bindings"}
    finally:
        await manager.close()
