import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from sandbox.app import Worker


@pytest.mark.asyncio
async def test_prompt_timeout_disconnects_and_preserves_metadata(tmp_path, monkeypatch):
    worker = Worker(tmp_path, tmp_path / "sessions")
    worker.db.execute("INSERT INTO sessions(id) VALUES ('test')")
    worker.db.commit()
    closed = []
    async def fail(prompt, timeout):
        raise TimeoutError
    async def close():
        closed.append(True)
    rpc = SimpleNamespace(prompt=fail, close=close)
    worker.connections["test"] = rpc
    async def connection(sid):
        return rpc
    monkeypatch.setattr(worker, "connection", connection)
    with pytest.raises(TimeoutError):
        await worker.prompt("test", "slow operation")
    assert closed == [True]
    assert worker.uid("test") == 10001
    assert not worker.connections
    await worker.close()


@pytest.mark.asyncio
async def test_worker_serializes_session_and_allows_others(tmp_path, monkeypatch):
    worker = Worker(tmp_path, tmp_path / "sessions")
    worker.db.executemany("INSERT INTO sessions(id) VALUES (?)", [("a",), ("b",)])
    active = set()
    max_active = 0
    async def connection(sid):
        async def prompt(text, timeout):
            nonlocal max_active
            assert sid not in active
            active.add(sid)
            max_active = max(max_active, len(active))
            await asyncio.sleep(.01)
            active.remove(sid)
            return {"output": sid}
        return SimpleNamespace(prompt=prompt)
    monkeypatch.setattr(worker, "connection", connection)
    results = await asyncio.gather(*(worker.prompt(sid, "test") for sid in ("a", "a", "b")))
    assert max_active == 2
    assert [r["output"] for r in results] == ["a", "a", "b"]
    await worker.close()


@pytest.mark.asyncio
async def test_capacity_rejection_does_not_create_record(tmp_path, monkeypatch):
    worker = Worker(tmp_path, tmp_path / "sessions")
    monkeypatch.setenv("MAX_SESSIONS", "0")
    with pytest.raises(HTTPException) as exc:
        await worker.create("unallocated")
    assert exc.value.status_code == 409
    assert worker.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    await worker.close()
