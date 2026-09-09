import asyncio
import hmac
import logging
import os
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from uuid import UUID
from weakref import WeakValueDictionary

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

from sandbox.isolation import provision
from sandbox.process import run_isolated
from sandbox.rpc import PiError, PiRPC


class Worker:
    def __init__(self, state=Path("/state"), root=Path("/sessions")):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o711)
        state.mkdir(parents=True, exist_ok=True)
        state.chmod(0o700)
        self.db = sqlite3.connect(state / "worker.sqlite")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sessions (slot INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL)"
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
        with self.db:
            if "last_activity" not in columns:
                self.db.execute(
                    "ALTER TABLE sessions ADD COLUMN last_activity REAL NOT NULL DEFAULT 0"
                )
            self.db.execute(
                "UPDATE sessions SET last_activity=? WHERE last_activity=0", (time.time(),)
            )
        self.locks = WeakValueDictionary()
        self.cleanup_task = None
        self.connections = {}
        self.allocation_lock = asyncio.Lock()

    def lock(self, session_id):
        return self.locks.setdefault(session_id, asyncio.Lock())

    def uid(self, session_id):
        row = self.db.execute("SELECT slot FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Unknown session")
        return 10000 + row[0]

    async def connection(self, session_id):
        uid = self.uid(session_id)
        rpc = self.connections.get(session_id)
        if rpc and (rpc.process.returncode is not None or rpc.reader.done()):
            await self.disconnect(session_id)
            rpc = None
        if not rpc:
            rpc = PiRPC()
            await rpc.start(self.root / session_id, uid)
            self.connections[session_id] = rpc
        return rpc

    async def disconnect(self, session_id):
        rpc = self.connections.pop(session_id, None)
        if rpc:
            await rpc.close()

    async def create(self, session_id):
        async with self.activity(session_id), self.allocation_lock:
            row = self.db.execute("SELECT slot FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                count = self.db.execute("SELECT count(*) FROM sessions").fetchone()[0]
                if count >= int(os.getenv("MAX_SESSIONS", "8")):
                    raise HTTPException(409, "Sandbox session capacity reached")
                with self.db:
                    used = {row[0] for row in self.db.execute("SELECT slot FROM sessions")}
                    slot = next((i for i in range(1, 55536) if i not in used), None)
                    if slot is None:
                        raise HTTPException(409, "Sandbox UID capacity reached")
                    self.db.execute(
                        "INSERT INTO sessions(slot, id, last_activity) VALUES (?, ?, ?)",
                        (slot, session_id, time.time()),
                    )
                try:
                    provision(self.root / session_id, 10000 + slot)
                except BaseException:
                    with self.db:
                        self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                    raise
            # Idempotent allocation allows the orchestrator to recover uncertain creates.
            # Failure keeps the row so DELETE/retry can clean up or reconnect it.
            try:
                await self.connection(session_id)
            except Exception as exc:
                logging.getLogger(__name__).error("Session %s startup failed: %s", session_id, exc)
                raise HTTPException(
                    503, "Pi startup failed; check namespace support and image configuration"
                ) from exc
            return {"session_id": session_id, "sandbox_id": os.getenv("SANDBOX_ID", "sandbox")}

    async def prompt(self, session_id, prompt):
        async with self.activity(session_id):
            self.uid(session_id)
            try:
                rpc = await self.connection(session_id)
                return await rpc.prompt(prompt, float(os.getenv("PROMPT_TIMEOUT", "180")))
            except (TimeoutError, asyncio.CancelledError):
                await self.disconnect(session_id)
                raise
            except (PiError, OSError) as exc:
                await self.disconnect(session_id)
                raise HTTPException(502, str(exc)) from exc

    async def delete(self, session_id, idle_before=None):
        lock = self.lock(session_id)
        if idle_before is not None and lock.locked():
            raise HTTPException(409, "Session is active")
        async with lock:
            row = self.db.execute(
                "SELECT last_activity FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if idle_before is not None and row and row[0] > idle_before:
                raise HTTPException(409, "Session was recently active")
            await self.disconnect(session_id)
            directory = self.root / session_id
            if directory.exists():
                shutil.rmtree(directory)
            with self.db:
                self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    async def resources(self, session_id, reload=False):
        async with self.activity(session_id):
            uid = self.uid(session_id)
            if reload:
                await self.disconnect(session_id)
            try:
                rpc = await self.connection(session_id)
                commands = await rpc.request("get_commands")
                packages = await run_isolated(
                    self.root / session_id, uid, ["pi", "list"], timeout=30
                )
                if packages["exit_code"]:
                    raise HTTPException(502, packages)
                return {
                    "session_id": session_id,
                    "commands": commands["commands"],
                    "packages": packages["output"],
                }
            except (TimeoutError, asyncio.CancelledError):
                await self.disconnect(session_id)
                raise
            except PiError as exc:
                await self.disconnect(session_id)
                raise HTTPException(502, str(exc)) from exc

    async def package(self, session_id, request):
        async with self.activity(session_id):
            uid = self.uid(session_id)
            # Avoid mutating settings while another pi process is reading/writing them.
            await self.disconnect(session_id)
            try:
                result = await run_isolated(
                    self.root / session_id, uid, ["pi", request.action, request.source]
                )
                if result["exit_code"]:
                    raise HTTPException(400, result)
                rpc = await self.connection(session_id)
                commands = await rpc.request("get_commands")
                return {
                    **result,
                    "action": request.action,
                    "source": request.source,
                    "reloaded": True,
                    "commands": commands["commands"],
                }
            except (TimeoutError, asyncio.CancelledError):
                await self.disconnect(session_id)
                raise
            except PiError as exc:
                await self.disconnect(session_id)
                raise HTTPException(502, f"Package changed but pi reload failed: {exc}") from exc

    @asynccontextmanager
    async def activity(self, session_id):
        async with self.lock(session_id):
            try:
                yield
            finally:
                with self.db:
                    self.db.execute(
                        "UPDATE sessions SET last_activity=? WHERE id=?", (time.time(), session_id)
                    )

    async def suspend_idle(self, now=None):
        ttl = int(os.getenv("PI_IDLE_DISCONNECT_SECONDS", "300"))
        if ttl <= 0:
            return []
        cutoff = (time.time() if now is None else now) - ttl
        suspended = []
        for session_id in list(self.connections):
            lock = self.lock(session_id)
            if lock.locked():
                continue
            async with lock:
                row = self.db.execute(
                    "SELECT last_activity FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if row and row[0] <= cutoff:
                    await self.disconnect(session_id)
                    suspended.append(session_id)
        return suspended

    def start_cleanup(self):
        interval = int(os.getenv("SESSION_CLEANUP_INTERVAL_SECONDS", "60"))
        if interval > 0:
            self.cleanup_task = asyncio.create_task(self.cleanup_loop(interval))

    async def cleanup_loop(self, interval):
        while True:
            await asyncio.sleep(interval)
            try:
                await self.suspend_idle()
            except Exception:
                logging.getLogger(__name__).exception("Idle Pi suspension failed")

    async def close(self):
        if self.cleanup_task:
            self.cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.cleanup_task
        await asyncio.gather(
            *(rpc.close() for rpc in self.connections.values()), return_exceptions=True
        )
        self.db.close()


@asynccontextmanager
async def lifespan(app):
    app.state.worker = Worker()
    app.state.worker.start_cleanup()
    try:
        yield
    finally:
        await app.state.worker.close()


app = FastAPI(title="Pi sandbox worker", lifespan=lifespan)


def authorize(authorization: str = Header(default="")):
    if not hmac.compare_digest(
        authorization, "Bearer " + os.getenv("SANDBOX_TOKEN", "worker-demo-change-me")
    ):
        raise HTTPException(401, "Invalid worker token")


class Prompt(BaseModel):
    prompt: str = Field(min_length=1, max_length=32000)


class PackageRequest(BaseModel):
    action: Literal["install", "remove"]
    source: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def source_supported(self):
        # Pass source as one argv element, never interpolate it into shell code.
        if any(c in self.source for c in ("\x00", "\n", "\r")) or not self.source.startswith(
            ("npm:", "git:", "https://", "http://", "/workspace/")
        ):
            raise ValueError("Use an npm:, git:, HTTPS/HTTP source or an absolute /workspace/ path")
        return self


@app.get("/health")
async def health():
    return {"status": "ok", "sandbox_id": os.getenv("SANDBOX_ID", "sandbox")}


@app.put("/sessions/{session_id}", dependencies=[Depends(authorize)])
async def create(session_id: UUID):
    return await app.state.worker.create(str(session_id))


@app.post("/sessions/{session_id}/prompt", dependencies=[Depends(authorize)])
async def prompt(session_id: UUID, body: Prompt):
    try:
        return await app.state.worker.prompt(str(session_id), body.prompt)
    except TimeoutError as exc:
        raise HTTPException(
            504, "Pi prompt timed out; process stopped; side effects may have occurred"
        ) from exc


@app.get("/sessions/{session_id}/resources", dependencies=[Depends(authorize)])
async def resources(session_id: UUID):
    try:
        return await app.state.worker.resources(str(session_id))
    except TimeoutError as exc:
        raise HTTPException(504, "Resource inspection timed out") from exc


@app.post("/sessions/{session_id}/resources/reload", dependencies=[Depends(authorize)])
async def reload_resources(session_id: UUID):
    try:
        return await app.state.worker.resources(str(session_id), reload=True)
    except TimeoutError as exc:
        raise HTTPException(504, "Resource reload timed out") from exc


@app.post("/sessions/{session_id}/packages", dependencies=[Depends(authorize)])
async def package(session_id: UUID, body: PackageRequest):
    try:
        return await app.state.worker.package(str(session_id), body)
    except TimeoutError as exc:
        raise HTTPException(
            504, "Package operation timed out; partial changes may remain; inspect before retrying"
        ) from exc


@app.delete("/sessions/{session_id}", dependencies=[Depends(authorize)])
async def delete(session_id: UUID, idle_before: float | None = None):
    await app.state.worker.delete(str(session_id), idle_before=idle_before)
    return {"deleted": str(session_id)}
