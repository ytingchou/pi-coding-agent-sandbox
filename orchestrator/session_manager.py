"""Persistent Agent -> worker/session routing, plus pooled HTTP connections.

Single API process by design. A distributed version needs database leases, not
just more Uvicorn workers; see README.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import suppress
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
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(bindings)")}
        with self.db:
            for name, definition in {
                "retention": "TEXT NOT NULL DEFAULT 'managed'",
                "idle_ttl_seconds": "INTEGER NOT NULL DEFAULT 0",
                "last_activity": "REAL NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE bindings ADD COLUMN {name} {definition}")
            self.db.execute(
                "UPDATE bindings SET last_activity=? WHERE last_activity=0", (time.time(),)
            )
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

    async def allocate(self, agent_id, sandbox_id=None, retention="managed", idle_ttl_seconds=None):
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
                "INSERT INTO bindings(id, agent_id, sandbox_id, status, retention, idle_ttl_seconds, last_activity) "
                "VALUES (?, ?, ?, 'allocating', ?, ?, ?)",
                (
                    session_id,
                    agent_id,
                    sandbox_id,
                    retention,
                    self.ttl(retention, idle_ttl_seconds),
                    time.time(),
                ),
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
        try:
            await self.call(binding["sandbox_id"], "PUT", f"/sessions/{session_id}")
        finally:
            self.touch(session_id)
        with self.db:
            self.db.execute("UPDATE bindings SET status='ready' WHERE id=?", (session_id,))
        return self.binding(agent_id, session_id)

    async def prompt(self, agent_id, session_id, prompt):
        binding = self.binding(agent_id, session_id)
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
            self.touch(session_id)

    async def delete(self, agent_id, session_id, idle_before=None):
        binding = self.binding(agent_id, session_id)
        with self.db:
            self.db.execute("UPDATE bindings SET status='deleting' WHERE id=?", (session_id,))
        try:
            await self.call(
                binding["sandbox_id"],
                "DELETE",
                f"/sessions/{session_id}",
                params={"idle_before": idle_before} if idle_before is not None else {},
            )
        except HTTPException as exc:
            if exc.status_code == 409 and idle_before is not None:
                with self.db:
                    self.db.execute(
                        "UPDATE bindings SET status=? WHERE id=?", (binding["status"], session_id)
                    )
                self.touch(session_id)
            raise
        with self.db:
            self.db.execute("DELETE FROM bindings WHERE id=?", (session_id,))

    async def resource_call(self, agent_id, session_id, method, path, **kwargs):
        binding = self.binding(agent_id, session_id)
        if binding["status"] != "ready":
            raise HTTPException(409, "Session not ready; reconnect first")
        try:
            return await self.call(
                binding["sandbox_id"], method, f"/sessions/{session_id}/{path}", **kwargs
            )
        finally:
            self.touch(session_id)

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

    def touch(self, session_id):
        with self.db:
            self.db.execute(
                "UPDATE bindings SET last_activity=? WHERE id=?", (time.time(), session_id)
            )

    def retention(self, agent_id, session_id, policy, seconds=None):
        binding = self.binding(agent_id, session_id)
        if binding["status"] == "deleting":
            raise HTTPException(409, "Session deletion is pending")
        with self.db:
            self.db.execute(
                "UPDATE bindings SET retention=?, idle_ttl_seconds=?, last_activity=? WHERE id=?",
                (policy, self.ttl(policy, seconds), time.time(), session_id),
            )
        return self.binding(agent_id, session_id)

    async def cleanup_once(self, dry_run=False):
        now = time.time()
        rows = [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM bindings WHERE status='deleting' OR "
                "(retention='ephemeral' AND last_activity+idle_ttl_seconds<=?)",
                (now,),
            )
        ]
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
                current_row = self.db.execute(
                    "SELECT * FROM bindings WHERE id=?", (row["id"],)
                ).fetchone()
                if current_row is None:
                    continue
                current = dict(current_row)
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
        self.db.close()
