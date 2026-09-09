import hmac
import os
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from agents.exceptions import AgentsException
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from orchestrator.agent import run_agent
from orchestrator.session_manager import SessionManager


@asynccontextmanager
async def lifespan(app):
    app.state.manager = SessionManager()
    app.state.manager.start_cleanup()
    try:
        yield
    finally:
        await app.state.manager.close()


app = FastAPI(title="OpenAI Agents SDK + Pi sandbox sample", lifespan=lifespan)


def authorize(authorization: str = Header(default="")):
    if not hmac.compare_digest(
        authorization, "Bearer " + os.getenv("API_TOKEN", "local-demo-change-me")
    ):
        raise HTTPException(401, "Invalid API token")


class RetentionRequest(BaseModel):
    retention: Literal["managed", "ephemeral"] = "managed"
    idle_ttl_seconds: int | None = Field(default=None, ge=1)


class SessionRequest(RetentionRequest):
    sandbox_id: str | None = None


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=32000)
    session_ids: list[UUID] | None = None


class PiPromptRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=32000)


class PackageRequest(BaseModel):
    action: Literal["install", "remove"]
    source: str = Field(min_length=1, max_length=512)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/sandboxes", dependencies=[Depends(authorize)])
async def sandboxes():
    return await app.state.manager.health()


@app.post("/agents", dependencies=[Depends(authorize)])
async def create_agent():
    return app.state.manager.create_agent()


@app.get("/agents/{agent_id}/sessions", dependencies=[Depends(authorize)])
async def list_sessions(agent_id: UUID):
    return app.state.manager.sessions(str(agent_id))


@app.post("/agents/{agent_id}/sessions", dependencies=[Depends(authorize)])
async def create_session(agent_id: UUID, body: SessionRequest):
    manager = app.state.manager
    async with manager.lock(str(agent_id)):
        return await manager.allocate(
            str(agent_id), body.sandbox_id, body.retention, body.idle_ttl_seconds
        )


@app.post("/agents/{agent_id}/sessions/{session_id}/connect", dependencies=[Depends(authorize)])
async def connect(agent_id: UUID, session_id: UUID):
    manager = app.state.manager
    async with manager.lock(str(agent_id)):
        return await manager.connect(str(agent_id), str(session_id))


@app.delete("/agents/{agent_id}/sessions/{session_id}", dependencies=[Depends(authorize)])
async def delete(agent_id: UUID, session_id: UUID):
    manager = app.state.manager
    async with manager.lock(str(agent_id)):
        await manager.delete(str(agent_id), str(session_id))
    return {"deleted": str(session_id)}


@app.get("/agents/{agent_id}/sessions/{session_id}/resources", dependencies=[Depends(authorize)])
async def resources(agent_id: UUID, session_id: UUID):
    manager = app.state.manager
    async with manager.lock(str(agent_id)):
        return await manager.resource_call(str(agent_id), str(session_id), "GET", "resources")


@app.post(
    "/agents/{agent_id}/sessions/{session_id}/resources/reload", dependencies=[Depends(authorize)]
)
async def reload_resources(agent_id: UUID, session_id: UUID):
    manager = app.state.manager
    async with manager.lock(str(agent_id)):
        return await manager.resource_call(
            str(agent_id), str(session_id), "POST", "resources/reload"
        )


@app.post("/agents/{agent_id}/sessions/{session_id}/packages", dependencies=[Depends(authorize)])
async def package(agent_id: UUID, session_id: UUID, body: PackageRequest):
    manager = app.state.manager
    async with manager.lock(str(agent_id)):
        return await manager.resource_call(
            str(agent_id), str(session_id), "POST", "packages", json=body.model_dump()
        )


@app.post("/agents/{agent_id}/sessions/{session_id}/pi/prompt", dependencies=[Depends(authorize)])
async def pi_prompt(agent_id: UUID, session_id: UUID, body: PiPromptRequest):
    """Direct pi entrypoint for slash commands and skills; /run still uses Agents SDK."""
    manager = app.state.manager
    async with manager.lock(str(agent_id)):
        return await manager.prompt(str(agent_id), str(session_id), body.prompt)


@app.post("/agents/{agent_id}/run", dependencies=[Depends(authorize)])
async def run(agent_id: UUID, body: RunRequest):
    manager = app.state.manager
    async with manager.lock(str(agent_id)):
        manager.agent(str(agent_id))
        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(503, "Set OPENAI_API_KEY in .env to run the live demo")
        try:
            return await run_agent(
                manager,
                str(agent_id),
                body.prompt,
                [str(sid) for sid in body.session_ids] if body.session_ids is not None else None,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except AgentsException as exc:
            details = getattr(exc, "run_data", None)
            usage = details.context_wrapper.usage if details else None
            raise HTTPException(
                502,
                {
                    "error": type(exc).__name__,
                    "usage": {
                        "requests": usage.requests,
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "total_tokens": usage.total_tokens,
                    }
                    if usage
                    else None,
                },
            ) from exc


@app.patch("/agents/{agent_id}/sessions/{session_id}/retention", dependencies=[Depends(authorize)])
async def retention(agent_id: UUID, session_id: UUID, body: RetentionRequest):
    manager = app.state.manager
    async with manager.lock(str(agent_id)):
        return manager.retention(
            str(agent_id), str(session_id), body.retention, body.idle_ttl_seconds
        )


@app.get("/sessions/cleanup", dependencies=[Depends(authorize)])
async def cleanup_status():
    return app.state.manager.cleanup_status


@app.post("/sessions/cleanup", dependencies=[Depends(authorize)])
async def cleanup(dry_run: bool = True):
    return await app.state.manager.cleanup_once(dry_run=dry_run)
