import asyncio
import json
import pytest

from sandbox.rpc import PiError, PiRPC


class Input:
    def __init__(self, output, events):
        self.output = output
        self.events = events

    def write(self, data):
        request = json.loads(data)
        for event in self.events:
            self.output.feed_data((json.dumps(event) + "\n").encode())
        self.output.feed_data((json.dumps({"type": "response", "id": request["id"], "success": True}) + "\n").encode())

    async def drain(self):
        pass


def rpc_with_events(events):
    rpc = PiRPC()
    class Process:
        returncode = None
        stdout = asyncio.StreamReader()
    rpc.process = Process()
    rpc.process.stdin = Input(rpc.process.stdout, events)
    rpc.reader = asyncio.create_task(rpc._read())
    return rpc


@pytest.mark.asyncio
async def test_agent_end_before_ack_and_error_message():
    for stop, expected in [("stop", "answer"), ("error", None)]:
        rpc = rpc_with_events([{"type": "agent_end", "messages": [{"role": "assistant", "stopReason": stop,
            "errorMessage": "model failed", "content": [{"type": "text", "text": "answer"}]}]}])
        try:
            if expected:
                assert (await rpc.prompt("test", 1))["output"] == expected
            else:
                with pytest.raises(PiError, match="model failed"):
                    await rpc.prompt("test", 1)
        finally:
            rpc.process.stdout.feed_eof()
            await rpc.reader


@pytest.mark.asyncio
async def test_ack_is_not_completion_and_dead_stream_fails():
    rpc = rpc_with_events([])
    with pytest.raises(TimeoutError):
        await rpc.prompt("test", .02)
    assert rpc.turn is None
    rpc.process.stdout.feed_eof()
    await rpc.reader
    with pytest.raises(PiError, match="not running"):
        await rpc.request("get_state")
