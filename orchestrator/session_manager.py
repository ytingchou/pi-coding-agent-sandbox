"""Persistent Agent -> worker/session routing, plus pooled HTTP connections.

Single API process by design. A distributed version needs database leases, not
just more Uvicorn workers; see README.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import suppress

import httpx
from fastapi import HTTPException

from orchestrator.registry import MongoRegistry


class SessionManager:
    def __init__(self, registry=None, endpoints=None, client=None):
        self.registry = registry if registry is not None else MongoRegistry()
        self.cleanup_task = None
        self.cleanup_status = {"last_run": None, "deleted": [], "failed": []}
        self.endpoints = endpoints or json.loads(os.getenv("SANDBOX_ENDPOINTS", "{}"))
        if not self.endpoints:
            raise ValueError("SANDBOX_ENDPOINTS must contain at least one worker")
        self.client = client or httpx.AsyncClient(
            headers={
                "Authorization": "Bearer " + os.getenv("SANDBOX_TOKEN", "worker-demo-change-me")
            },
            timeout=httpx.Timeout(float(os.getenv("PROMPT_TIMEOUT", "180")) + 60, connect=5),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        self.locks = {}

    def lock(self, agent_id):
        return self.locks.setdefault(agent_id, asyncio.Lock())

    async def start(self):
        await self.registry.initialize()
        self.start_cleanup()

    async def agent(self, agent_id):
        if not await self.registry.get_agent(agent_id):
            raise HTTPException(404, "Unknown agent")

    async def create_agent(self):
        agent_id = str(uuid.uuid4())
        await self.registry.insert_agent(agent_id)
        return {"agent_id": agent_id}

    async def sessions(self, agent_id):
        await self.agent(agent_id)
        return await self.registry.sessions(agent_id)

    async def binding(self, agent_id, session_id):
        await self.agent(agent_id)
        row = await self.registry.binding(session_id, agent_id)
        if not row:
            raise HTTPException(404, "Session does not belong to this agent")
        return row

    async def call(self, sandbox_id, method, path, **kwargs):
        base = self.endpoints.get(sandbox_id)
        if not base:
            raise HTTPException(503, "Bound sandbox is not configured; restore its endpoint")
        try:
            response = await self.client.request(method, base.rstrip("/") + path, **kwargs)
        except httpx.TimeoutException as exc:
            raise HTTPException(
                504, "Sandbox connection timed out; request was not retried and may have executed"
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                503, "Sandbox unavailable; existing sessions are never moved silently"
            ) from exc
        if response.is_error:
            detail = response.json().get("detail", "Sandbox request failed")
            raise HTTPException(response.status_code, detail)
        return response.json()

    async def allocate(self, agent_id, sandbox_id=None, retention="managed", idle_ttl_seconds=None):
        await self.agent(agent_id)
        if sandbox_id is None:
            healthy = [item["sandbox_id"] for item in await self.health() if item["healthy"]]
            if not healthy:
                raise HTTPException(503, "No healthy sandbox is available")
            counts = await self.registry.counts()
            sandbox_id = min(healthy, key=lambda name: counts.get(name, 0))
        if sandbox_id not in self.endpoints:
            raise HTTPException(400, "Unknown sandbox_id")
        session_id = str(uuid.uuid4())
        ttl = self.ttl(retention, idle_ttl_seconds)
        now = time.time()
        await self.registry.insert(
            session_id,
            agent_id=agent_id,
            sandbox_id=sandbox_id,
            status="allocating",
            retention=retention,
            idle_ttl_seconds=ttl,
            created_at=now,
            last_activity=now,
            expires_at=now + ttl if ttl else None,
        )
        try:
            await self.connect(agent_id, session_id)
        except HTTPException as exc:
            # Keep uncertain allocations addressable for explicit reconnect/delete.
            raise HTTPException(
                exc.status_code,
                {"message": exc.detail, "session_id": session_id, "sandbox_id": sandbox_id},
            ) from exc
        return await self.binding(agent_id, session_id)

    async def connect(self, agent_id, session_id):
        binding = await self.binding(agent_id, session_id)
        if binding["status"] == "deleting":
            raise HTTPException(409, "Session deletion is pending; retry DELETE")
        try:
            await self.call(binding["sandbox_id"], "PUT", f"/sessions/{session_id}")
        finally:
            await self.touch(session_id)
        await self.registry.update(session_id, status="ready")
        return await self.binding(agent_id, session_id)

    async def prompt(self, agent_id, session_id, prompt):
        binding = await self.binding(agent_id, session_id)
        if binding["status"] != "ready":
            raise HTTPException(409, "Session not ready; reconnect first")
        try:
            return await self.call(
                binding["sandbox_id"],
                "POST",
                f"/sessions/{session_id}/prompt",
                json={"prompt": prompt},
            )
        finally:
            await self.touch(session_id)

    async def delete(self, agent_id, session_id, idle_before=None):
        binding = await self.binding(agent_id, session_id)
        await self.registry.update(session_id, status="deleting")
        try:
            await self.call(
                binding["sandbox_id"],
                "DELETE",
                f"/sessions/{session_id}",
                params={"idle_before": idle_before} if idle_before is not None else {},
            )
        except HTTPException as exc:
            if exc.status_code == 409 and idle_before is not None:
                await self.registry.update(session_id, status=binding["status"])
                await self.touch(session_id)
            raise
        await self.registry.delete(session_id)

    async def resource_call(self, agent_id, session_id, method, path, **kwargs):
        binding = await self.binding(agent_id, session_id)
        if binding["status"] != "ready":
            raise HTTPException(409, "Session not ready; reconnect first")
        try:
            return await self.call(
                binding["sandbox_id"], method, f"/sessions/{session_id}/{path}", **kwargs
            )
        finally:
            await self.touch(session_id)

    @staticmethod
    def ttl(retention, seconds):
        if retention not in {"managed", "ephemeral"}:
            raise HTTPException(400, "Unknown retention policy")
        if retention == "managed":
            return 0
        value = (
            seconds if seconds is not None else int(os.getenv("SESSION_IDLE_TTL_SECONDS", "3600"))
        )
        if value <= 0:
            raise HTTPException(400, "Ephemeral sessions require a positive idle TTL")
        return value

    async def touch(self, session_id):
        row = await self.registry.binding(session_id)
        if row:
            now = time.time()
            ttl = row["idle_ttl_seconds"]
            await self.registry.update(
                session_id, last_activity=now, expires_at=now + ttl if ttl else None
            )

    async def retention(self, agent_id, session_id, policy, seconds=None):
        binding = await self.binding(agent_id, session_id)
        if binding["status"] == "deleting":
            raise HTTPException(409, "Session deletion is pending")
        ttl = self.ttl(policy, seconds)
        now = time.time()
        await self.registry.update(
            session_id,
            retention=policy,
            idle_ttl_seconds=ttl,
            last_activity=now,
            expires_at=now + ttl if ttl else None,
        )
        return await self.binding(agent_id, session_id)

    async def cleanup_once(self, dry_run=False):
        now = time.time()
        rows = await self.registry.expired(now)
        result = {
            "last_run": now,
            "dry_run": dry_run,
            "candidates": [],
            "deleted": [],
            "failed": [],
        }
        for row in rows:
            lock = self.lock(row["agent_id"])
            if lock.locked():
                continue
            async with lock:
                # A concurrent manual sweep may have deleted a later snapshot row.
                current = await self.registry.binding(row["id"])
                if current is None:
                    continue
                pending = current["status"] == "deleting"
                if not pending and (
                    current["retention"] != "ephemeral"
                    or current["last_activity"] + current["idle_ttl_seconds"] > now
                ):
                    continue
                result["candidates"].append(row["id"])
                if dry_run:
                    continue
                try:
                    await self.delete(
                        row["agent_id"],
                        row["id"],
                        idle_before=now if pending else now - current["idle_ttl_seconds"],
                    )
                    result["deleted"].append(row["id"])
                    logging.getLogger(__name__).info("Session cleaned: %s", row["id"])
                except HTTPException as exc:
                    result["failed"].append({"session_id": row["id"], "status": exc.status_code})
        self.cleanup_status = result
        return result

    def start_cleanup(self):
        interval = int(os.getenv("SESSION_CLEANUP_INTERVAL_SECONDS", "60"))
        if interval > 0:
            self.cleanup_task = asyncio.create_task(self.cleanup_loop(interval))

    async def cleanup_loop(self, interval):
        while True:
            await asyncio.sleep(interval)
            try:
                await self.cleanup_once()
            except Exception:
                logging.getLogger(__name__).exception("Session cleanup sweep failed")

    async def health(self):
        async def check(name):
            try:
                response = await self.client.get(
                    self.endpoints[name].rstrip("/") + "/health", timeout=3
                )
                response.raise_for_status()
                return {"sandbox_id": name, "healthy": True}
            except httpx.HTTPError:
                return {"sandbox_id": name, "healthy": False}

        return await asyncio.gather(*(check(name) for name in self.endpoints))

    async def close(self):
        if self.cleanup_task:
            self.cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.cleanup_task
        await self.client.aclose()
        await self.registry.close()
