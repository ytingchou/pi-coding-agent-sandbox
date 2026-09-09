"""Offline resource demo; add --live to include actual pi/Agents SDK model turns."""
import argparse
import asyncio
import json
import os

import httpx


async def main(live=False, keep=False):
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=600,
        headers={"Authorization": "Bearer " + os.getenv("API_TOKEN", "local-demo-change-me")}) as client:
        async def call(method, path, payload=None):
            response = await client.request(method, path, json=payload)
            if response.is_error:
                raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
            return response.json()
        def show(label, value):
            print(label + ": " + json.dumps(value, ensure_ascii=False, indent=2), flush=True)
        agent = (await call("POST", "/agents"))["agent_id"]
        sessions = []
        try:
            for worker in ("sandbox-1", "sandbox-1", "sandbox-2"):
                sessions.append(await call("POST", f"/agents/{agent}/sessions", {"sandbox_id": worker}))
            paths = [f"/agents/{agent}/sessions/{s['id']}" for s in sessions]
            show("Sessions", sessions)
            resources = await call("GET", paths[0] + "/resources")
            names = {c["name"] for c in resources["commands"]}
            assert {"skill:python-stats", "session-info"} <= names, resources
            assert "package-stats" not in names
            show("Skill and extension discovered", resources)

            info = await call("POST", paths[0] + "/pi/prompt", {"prompt": "/session-info"})
            assert info["kind"] == "extension_command"
            assert "session_info" in json.loads(info["output"])["tools"]
            show("Extension command (no model)", info)

            package = {"action": "install", "source": "/workspace/packages/stats-kit"}
            installed = await call("POST", paths[0] + "/packages", package)
            names = {c["name"] for c in installed["commands"]}
            assert {"package-stats", "skill:package-stats", "package-report"} <= names, installed
            show("Native pi package installed and reloaded", installed)
            result = await call("POST", paths[0] + "/pi/prompt", {"prompt": "/package-stats 2,4,6,8"})
            assert json.loads(result["output"])["mean"] == 5
            show("Package extension executes real Python (no model)", result)

            for path in paths[1:]:
                sibling = await call("GET", path + "/resources")
                assert "package-stats" not in {c["name"] for c in sibling["commands"]}, sibling
            print("PASS: package installation is isolated from the other two sessions", flush=True)
            await call("POST", paths[0] + "/resources/reload")
            after_reload = await call("POST", paths[0] + "/pi/prompt", {"prompt": "/package-stats 10,20"})
            assert json.loads(after_reload["output"])["mean"] == 15
            print("PASS: package survives Pi process reload", flush=True)

            if live:
                # Force native skill expansion in pi; this requires OPENAI_API_KEY.
                show("Live skill writes and executes Python", await call("POST", paths[0] + "/pi/prompt",
                    {"prompt": "/skill:python-stats 2,4,6,8"}))
                show("Live Agents SDK → pi package tool", await call("POST", f"/agents/{agent}/run", {
                    "session_ids": [sessions[0]["id"]],
                    "prompt": "Delegate to pi with the exact prompt '/skill:package-stats 2,4,6,8'. Ask pi to also call session_info. Return its actual tool results.",
                }))
            removed = await call("POST", paths[0] + "/packages", {**package, "action": "remove"})
            assert "package-stats" not in {c["name"] for c in removed["commands"]}, removed
            print("PASS: native pi remove unloads the package; standalone resources remain", flush=True)
            if not live:
                print("Offline demo complete. Skill model execution and Agents SDK model turns require --live.", flush=True)
        finally:
            if not keep:
                for session in sessions:
                    await call("DELETE", f"/agents/{agent}/sessions/{session['id']}")
                print("Demo sessions cleaned up", flush=True)
            else:
                print("Sessions retained (--keep). Use their IDs to continue or delete them.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Call real models; requires OPENAI_API_KEY")
    parser.add_argument("--keep", action="store_true", help="Retain demo sessions")
    args = parser.parse_args()
    asyncio.run(main(args.live, args.keep))
