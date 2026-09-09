import asyncio

import httpx
import pytest
from fastapi import HTTPException

from orchestrator.session_manager import SessionManager


@pytest.mark.asyncio
async def test_routing_ownership_persistence_and_cleanup(tmp_path):
    calls = []

    async def transport(request):
        calls.append((request.method, str(request.url)))
        return httpx.Response(200, json={"output": "42"})

    endpoints = {"one": "http://one", "two": "http://two"}
    manager = SessionManager(
        tmp_path, endpoints, httpx.AsyncClient(transport=httpx.MockTransport(transport))
    )
    agent = manager.create_agent()["agent_id"]
    other = manager.create_agent()["agent_id"]
    a = await manager.allocate(agent)
    b = await manager.allocate(agent)
    c = await manager.allocate(agent, "one")
    assert [s["sandbox_id"] for s in (a, b, c)] == ["one", "two", "one"]
    with pytest.raises(HTTPException) as exc:
        await manager.prompt(other, a["id"], "steal files")
    assert exc.value.status_code == 404
    await manager.close()
    manager = SessionManager(
        tmp_path, endpoints, httpx.AsyncClient(transport=httpx.MockTransport(transport))
    )
    assert len(manager.sessions(agent)) == 3
    assert (await manager.prompt(agent, a["id"], "continue"))["output"] == "42"
    assert calls[-1][1].startswith("http://one/")
    await manager.delete(agent, a["id"])
    assert len(manager.sessions(agent)) == 2
    await manager.close()


@pytest.mark.asyncio
async def test_uncertain_create_stays_recoverable_and_no_prompt_retry(tmp_path):
    calls = 0

    async def transport(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    manager = SessionManager(
        tmp_path, {"one": "http://one"}, httpx.AsyncClient(transport=httpx.MockTransport(transport))
    )
    agent = manager.create_agent()["agent_id"]
    with pytest.raises(HTTPException) as exc:
        await manager.allocate(agent, "one")
    session_id = exc.value.detail["session_id"]
    assert manager.binding(agent, session_id)["status"] == "allocating"
    assert calls == 1
    manager.db.execute("UPDATE bindings SET status='ready'")
    with pytest.raises(HTTPException):
        await manager.prompt(agent, session_id, "side effects")
    assert calls == 2
    await manager.close()


@pytest.mark.asyncio
async def test_agent_lock_serializes_same_agent_but_not_others(tmp_path):
    manager = SessionManager(tmp_path, {"one": "http://one"})
    entered = asyncio.Event()

    async def waiting():
        async with manager.lock("a"):
            entered.set()

    async with manager.lock("a"):
        task = asyncio.create_task(waiting())
        async with manager.lock("b"):
            await asyncio.sleep(0)
            assert not entered.is_set()
    await task
    assert entered.is_set()
    await manager.close()


@pytest.mark.asyncio
async def test_resource_operations_enforce_owner_and_sticky_route(tmp_path):
    calls = []

    async def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"commands": []})

    manager = SessionManager(
        tmp_path,
        {"one": "http://one", "two": "http://two"},
        httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )
    agent = manager.create_agent()["agent_id"]
    other = manager.create_agent()["agent_id"]
    session = await manager.allocate(agent, "two")
    await manager.resource_call(
        agent,
        session["id"],
        "POST",
        "packages",
        json={"action": "install", "source": "npm:example@1.0.0"},
    )
    assert str(calls[-1].url) == f"http://two/sessions/{session['id']}/packages"
    count = len(calls)
    with pytest.raises(HTTPException) as exc:
        await manager.resource_call(other, session["id"], "GET", "resources")
    assert exc.value.status_code == 404
    assert len(calls) == count
    manager.db.execute("UPDATE bindings SET status='deleting'")
    with pytest.raises(HTTPException) as exc:
        await manager.resource_call(agent, session["id"], "POST", "resources/reload")
    assert exc.value.status_code == 409
    assert len(calls) == count
    await manager.close()
