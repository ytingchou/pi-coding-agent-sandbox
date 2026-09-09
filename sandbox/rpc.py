"""A single long-lived Pi JSONL connection. The caller serializes prompt turns."""
import asyncio
import json
import os
import signal
import uuid
from collections import deque
from pathlib import Path

from sandbox import isolation


class PiError(RuntimeError):
    pass


class PiRPC:
    def __init__(self):
        self.process = None
        self.pending = {}
        self.turn = None
        self.reader = None
        self.stderr_reader = None
        self.stderr = deque(maxlen=20)
        self.tool_results = []
        self.notifications = []
        self.extension_errors = []

    async def start(self, root: Path, uid: int):
        self.process = await asyncio.create_subprocess_exec(
            *isolation.command(root, ["/bin/bash", "-c", isolation.BOOTSTRAP, "pi-bootstrap", os.getenv("PI_MODEL", "gpt-4.1-mini")]),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=isolation.environment(),
            user=uid, group=uid, extra_groups=[], preexec_fn=isolation.limits,
            start_new_session=True, limit=4 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self._read())
        self.stderr_reader = asyncio.create_task(self._read_stderr())
        try:
            await self.request("get_state", timeout=45)
        except Exception as exc:
            await self.close()
            detail = "".join(self.stderr)[-2000:]
            detail = isolation.redact(detail)
            raise PiError(f"Pi startup failed: {detail}") from exc
        except BaseException:
            await self.close()
            raise

    async def _read_stderr(self):
        while data := await self.process.stderr.read(4096):
            self.stderr.append(data.decode(errors="replace"))

    async def _read(self):
        failure = PiError("Pi RPC process closed")
        try:
            while line := await self.process.stdout.readline():
                event = json.loads(line)
                if event.get("type") == "response":
                    future = self.pending.get(event.get("id"))
                    if future and not future.done():
                        if event.get("success"):
                            future.set_result(event.get("data"))
                        else:
                            future.set_exception(PiError(event.get("error", "RPC rejected")))
                elif event.get("type") == "tool_execution_end" and self.turn:
                    # Bound returned evidence; Pi's full transcript stays in the session.
                    if len(self.tool_results) < 32:
                        self.tool_results.append({
                            "tool": event.get("toolName"), "is_error": event.get("isError", False),
                            "result": json.dumps(event.get("result"), ensure_ascii=False)[:8000],
                        })
                elif event.get("type") == "agent_end" and self.turn and not self.turn.done():
                    self.turn.set_result(event.get("messages", []))
                elif event.get("type") == "extension_ui_request" and event.get("method") == "notify" and self.turn:
                    if len(self.notifications) < 32:
                        self.notifications.append(str(event.get("message", ""))[:2000])
                elif event.get("type") == "extension_error" and self.turn and len(self.extension_errors) < 32:
                    self.extension_errors.append(str(event.get("error", "Extension failed"))[:2000])
        except Exception as exc:
            failure = PiError(f"Pi RPC stream failed: {type(exc).__name__}")
        finally:
            for future in [*self.pending.values(), self.turn]:
                if future and not future.done():
                    future.set_exception(failure)

    async def request(self, kind, timeout=10, **payload):
        if self.reader.done() or self.process.returncode is not None:
            raise PiError("Pi RPC is not running")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            self.process.stdin.write((json.dumps({"id": request_id, "type": kind, **payload}) + "\n").encode())
            await self.process.stdin.drain()
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def prompt(self, prompt: str, timeout: float):
        self.turn = asyncio.get_running_loop().create_future()
        self.tool_results = []
        self.notifications = []
        self.extension_errors = []
        try:
            # Register completion before sending: agent_end can race prompt acceptance.
            async with asyncio.timeout(timeout):
                extension_command = False
                history_size = 0
                if prompt.startswith("/"):
                    name = prompt.split(maxsplit=1)[0][1:]
                    commands = await self.request("get_commands")
                    extension_command = any(c["name"] == name and c["source"] == "extension" for c in commands["commands"])
                    if extension_command:
                        history_size = len((await self.request("get_messages"))["messages"])
                await self.request("prompt", timeout=timeout, message=prompt)
                if self.extension_errors:
                    raise PiError("; ".join(self.extension_errors))
                if extension_command and not self.turn.done():
                    state = await self.request("get_state")
                    if not state["isStreaming"]:
                        # Extension commands may finish without any model turn/agent_end.
                        messages = (await self.request("get_messages"))["messages"][history_size:]
                        texts = []
                        for message in messages:
                            content = message.get("content", "")
                            if isinstance(content, str):
                                texts.append(content)
                            elif isinstance(content, list):
                                texts.extend(c.get("text", "") for c in content if c.get("type") == "text")
                        return {"output": "\n".join(texts + self.notifications)[:32000],
                                "tool_results": self.tool_results, "kind": "extension_command"}
                messages = await self.turn
                assistants = [m for m in messages if m.get("role") == "assistant"]
                if not assistants:
                    raise PiError("Pi completed without an assistant result")
                last = assistants[-1]
                if last.get("stopReason") in ("error", "aborted"):
                    raise PiError(last.get("errorMessage", "Pi turn failed"))
                output = "\n".join(c.get("text", "") for c in last.get("content", []) if c.get("type") == "text")
                return {"output": output[:32000], "tool_results": self.tool_results}
        finally:
            if self.turn and not self.turn.done():
                self.turn.cancel()
            elif self.turn and not self.turn.cancelled():
                self.turn.exception()  # Consume a concurrent stream failure.
            self.turn = None

    async def close(self):
        if self.process and self.process.returncode is None:
            # Killing the namespace init also kills detached grandchildren in that namespace.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.process.wait()
        for task in (self.reader, self.stderr_reader):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.reader, self.stderr_reader) if t), return_exceptions=True)
