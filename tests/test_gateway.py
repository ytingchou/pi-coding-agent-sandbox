"""Real SDK → HTTP mock gateway → real Pi SSE/tools; no external network or tokens."""

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_ISOLATION_TESTS") != "1", reason="Requires sandbox image"
)


@pytest.mark.asyncio
async def test_independent_chat_gateways_route_both_agents_and_execute_python(
    tmp_path, monkeypatch
):
    from agents import set_tracing_disabled

    from orchestrator.agent import run_agent
    from sandbox.app import Worker

    set_tracing_disabled(True)
    sid = str(uuid.uuid4())
    requests = []
    code = "import requests, sqlite3\nfrom minio import Minio\nwith sqlite3.connect(':memory:') as db:\n    print(db.execute('SELECT 6 * 7').fetchone()[0])\n"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(
                {
                    "port": self.server.server_port,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": data,
                }
            )
            is_pi = data["model"] == "company-pi-model"
            count = sum(q["body"]["model"] == data["model"] for q in requests)
            if is_pi and count == 1:
                tool = {
                    "id": "pi_write",
                    "type": "function",
                    "function": {
                        "name": "write",
                        "arguments": json.dumps(
                            {"path": "/workspace/gateway_demo.py", "content": code}
                        ),
                    },
                }
            elif is_pi and count == 2:
                tool = {
                    "id": "pi_bash",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps({"command": "python /workspace/gateway_demo.py"}),
                    },
                }
            elif not is_pi and count == 1:
                tool = {
                    "id": "outer_delegate",
                    "type": "function",
                    "function": {
                        "name": "run_python_in_sandbox",
                        "arguments": json.dumps(
                            {
                                "session_id": sid,
                                "prompt": "Use preinstalled requests, minio and sqlite3. Write Python to compute 6 * 7 using SQLite and execute it.",
                            }
                        ),
                    },
                }
            else:
                tool = None
            usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
            base = {
                "id": f"chatcmpl_{len(requests)}",
                "created": 1700000000,
                "model": data["model"],
            }
            if data.get("stream"):
                delta = (
                    {"role": "assistant", "tool_calls": [{"index": 0, **tool}]}
                    if tool
                    else {"role": "assistant", "content": "42"}
                )
                chunks = [
                    {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    },
                    {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "tool_calls" if tool else "stop",
                            }
                        ],
                    },
                    {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage},
                ]
                body = (
                    "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
                ).encode()
                content_type = "text/event-stream"
            else:
                message = (
                    {"role": "assistant", "content": None, "tool_calls": [tool]}
                    if tool
                    else {"role": "assistant", "content": "42"}
                )
                body = json.dumps(
                    {
                        **base,
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": message,
                                "finish_reason": "tool_calls" if tool else "stop",
                            }
                        ],
                        "usage": usage,
                    }
                ).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    outer_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    pi_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    servers = (outer_server, pi_server)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    outer_url = f"http://127.0.0.1:{outer_server.server_port}/outer/v1"
    pi_url = f"http://127.0.0.1:{pi_server.server_port}/coding/v1"
    assert outer_url != pi_url
    for k, v in {
        "OPENAI_BASE_URL": outer_url,
        "OPENAI_API_KEY": "outer-test-key",
        "OPENAI_MODEL": "company-outer-model",
        "OPENAI_API_MODE": "chat_completions",
        "PI_BASE_URL": pi_url,
        "PI_API_KEY": "pi-test-key",
        "PI_MODEL": "company-pi-model",
        "PI_API_MODE": "chat_completions",
    }.items():
        monkeypatch.setenv(k, v)
    root = tmp_path / "sessions"
    root.mkdir(mode=0o711)
    for p in [tmp_path, *tmp_path.parents]:
        if p not in (Path("/"), Path("/tmp")):
            p.chmod(0o711)
    worker = Worker(tmp_path / "state", root)

    class Manager:
        async def sessions(self, aid):
            return [{"id": sid, "sandbox_id": "sandbox-1", "status": "ready"}]

        async def prompt(self, aid, session_id, prompt):
            assert session_id == sid
            return await worker.prompt(sid, prompt)

    try:
        await worker.create(sid)
        result = await run_agent(
            Manager(),
            "gateway-agent",
            "Compute 6 * 7",
            session_ids=[sid],
            history_path=str(tmp_path / "history.sqlite"),
        )
        assert result["output"] == "42"
        tools = result["sandbox_results"][0]["tool_results"]
        assert {t["tool"] for t in tools if not t["is_error"]} == {"write", "bash"}
        assert "42" in next(t["result"] for t in tools if t["tool"] == "bash")
        assert len(requests) == 5
        for q in requests:
            is_pi = q["body"]["model"] == "company-pi-model"
            assert q["port"] == (pi_server.server_port if is_pi else outer_server.server_port)
            assert q["path"] == (
                "/coding/v1/chat/completions" if is_pi else "/outer/v1/chat/completions"
            )
            assert q["authorization"] == (
                "Bearer pi-test-key" if is_pi else "Bearer outer-test-key"
            )
        pi_first = next(q for q in requests if q["body"]["model"] == "company-pi-model")
        system = json.dumps(pi_first["body"]["messages"])
        assert "Do not run pip install" in system
        assert "requests==" in system and "minio==" in system and "sqlite3" in system
    finally:
        await worker.close()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
