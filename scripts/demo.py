"""Run: docker compose exec api python scripts/demo.py"""
import asyncio
import json
import os

import httpx


async def main():
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=600,
        headers={"Authorization": "Bearer " + os.getenv("API_TOKEN", "local-demo-change-me")}) as client:
        async def post(path, payload):
            response = await client.post(path, json=payload)
            response.raise_for_status()
            return response.json()
        agent = (await post("/agents", {}))["agent_id"]
        sessions = [await post(f"/agents/{agent}/sessions", {"sandbox_id": name})
                    for name in ("sandbox-1", "sandbox-1", "sandbox-2")]
        print(json.dumps({"agent_id": agent, "sessions": sessions}, indent=2), flush=True)
        tasks = [
            "Write main.py to compute the sum of squares from 1 to 10, execute it, and save the value to result.txt.",
            "Check that result.txt is absent in this session. Then write and execute Python to calculate factorial(10).",
            "Write and execute Python to calculate the first 12 Fibonacci numbers.",
        ]
        for session, task in zip(sessions, tasks):
            result = await post(f"/agents/{agent}/run", {"prompt": task, "session_ids": [session["id"]]})
            print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
        result = await post(f"/agents/{agent}/run", {"prompt": "Read result.txt from the earlier run using Python and multiply it by 2. Execute the code.", "session_ids": [sessions[0]["id"]]})
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("Sessions retained for inspection. Use the documented DELETE endpoints to remove them.")


if __name__ == "__main__":
    asyncio.run(main())
