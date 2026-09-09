"""Persistent Agent -> worker/session routing, plus pooled HTTP connections.

Single API process by design. A distributed version needs database leases, not
just more Uvicorn workers; see README.
"""

import asyncio
import json
import os
import sqlite3
import uuid
from pathlib import Path

import httpx
from fastapi import HTTPException


class SessionManager:
    def __init__(self, state=Path("/state"), endpoints=None, client=None):
        state.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state / "registry.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS bindings(
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, sandbox_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'allocating');
        """)
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

    def agent(self, agent_id):
        if not self.db.execute("SELECT id FROM agents WHERE id=?", (agent_id,)).fetchone():
            raise HTTPException(404, "Unknown agent")

    def create_agent(self):
        agent_id = str(uuid.uuid4())
        with self.db:
            self.db.execute("INSERT INTO agents VALUES (?)", (agent_id,))
        return {"agent_id": agent_id}

    def sessions(self, agent_id):
        self.agent(agent_id)
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM bindings WHERE agent_id=? ORDER BY rowid", (agent_id,)
            )
        ]

    def binding(self, agent_id, session_id):
        self.agent(agent_id)
        row = self.db.execute(
            "SELECT * FROM bindings WHERE id=? AND agent_id=?", (session_id, agent_id)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Session does not belong to this agent")
        return dict(row)

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

    async def allocate(self, agent_id, sandbox_id=None):
        self.agent(agent_id)
        if sandbox_id is None:
            healthy = [item["sandbox_id"] for item in await self.health() if item["healthy"]]
            if not healthy:
                raise HTTPException(503, "No healthy sandbox is available")
            counts = dict(
                self.db.execute("SELECT sandbox_id, count(*) FROM bindings GROUP BY sandbox_id")
            )
            sandbox_id = min(healthy, key=lambda name: counts.get(name, 0))
        if sandbox_id not in self.endpoints:
            raise HTTPException(400, "Unknown sandbox_id")
        session_id = str(uuid.uuid4())
        with self.db:
            self.db.execute(
                "INSERT INTO bindings VALUES (?, ?, ?, 'allocating')",
                (session_id, agent_id, sandbox_id),
            )
        try:
            await self.connect(agent_id, session_id)
        except HTTPException as exc:
            # Keep uncertain allocations addressable for explicit reconnect/delete.
            raise HTTPException(
                exc.status_code,
                {"message": exc.detail, "session_id": session_id, "sandbox_id": sandbox_id},
            ) from exc
        return self.binding(agent_id, session_id)

    async def connect(self, agent_id, session_id):
        binding = self.binding(agent_id, session_id)
        if binding["status"] == "deleting":
            raise HTTPException(409, "Session deletion is pending; retry DELETE")
        await self.call(binding["sandbox_id"], "PUT", f"/sessions/{session_id}")
        with self.db:
            self.db.execute("UPDATE bindings SET status='ready' WHERE id=?", (session_id,))
        return self.binding(agent_id, session_id)

    async def prompt(self, agent_id, session_id, prompt):
        binding = self.binding(agent_id, session_id)
        if binding["status"] != "ready":
            raise HTTPException(409, "Session not ready; reconnect first")
        return await self.call(
            binding["sandbox_id"], "POST", f"/sessions/{session_id}/prompt", json={"prompt": prompt}
        )

    async def delete(self, agent_id, session_id):
        binding = self.binding(agent_id, session_id)
        with self.db:
            self.db.execute("UPDATE bindings SET status='deleting' WHERE id=?", (session_id,))
        await self.call(binding["sandbox_id"], "DELETE", f"/sessions/{session_id}")
        with self.db:
            self.db.execute("DELETE FROM bindings WHERE id=?", (session_id,))

    async def resource_call(self, agent_id, session_id, method, path, **kwargs):
        binding = self.binding(agent_id, session_id)
        if binding["status"] != "ready":
            raise HTTPException(409, "Session not ready; reconnect first")
        return await self.call(
            binding["sandbox_id"], method, f"/sessions/{session_id}/{path}", **kwargs
        )

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
        await self.client.aclose()
        self.db.close()
