"""Control-plane smoke test against both real workers; no LLM requests."""
import asyncio
import os

import httpx


async def main():
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=120,
        headers={"Authorization": "Bearer " + os.getenv("API_TOKEN", "local-demo-change-me")}) as client:
        async def call(method, path, body=None):
            response = await client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()
        health = await call("GET", "/sandboxes")
        assert all(row["healthy"] for row in health), health
        agent = (await call("POST", "/agents"))["agent_id"]
        other = (await call("POST", "/agents"))["agent_id"]
        sessions = []
        try:
            for target in ("sandbox-1", "sandbox-1", "sandbox-2"):
                sessions.append(await call("POST", f"/agents/{agent}/sessions", {"sandbox_id": target}))
            assert len(await call("GET", f"/agents/{agent}/sessions")) == 3
            for session in sessions:
                assert (await call("POST", f"/agents/{agent}/sessions/{session['id']}/connect"))["status"] == "ready"
            denied = await client.post(f"/agents/{other}/sessions/{sessions[0]['id']}/connect")
            assert denied.status_code == 404
            unauthorized = await client.post("/agents", headers={"Authorization": "Bearer invalid"})
            assert unauthorized.status_code == 401
            print("PASS: two healthy workers, three Pi sessions, reconnect, ownership, authentication")
        finally:
            for session in sessions:
                await call("DELETE", f"/agents/{agent}/sessions/{session['id']}")
            assert await call("GET", f"/agents/{agent}/sessions") == []
            print("PASS: all smoke-test sessions deleted")


if __name__ == "__main__":
    asyncio.run(main())
