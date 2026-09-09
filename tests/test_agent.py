import json

import pytest
from agents import Model, ModelResponse, Usage, set_tracing_disabled
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from orchestrator.agent import run_agent


class ScriptedModel(Model):
    def __init__(self, session_id):
        self.session_id = session_id
        self.calls = 0

    async def get_response(self, *args, **kwargs):
        self.calls += 1
        self.allowed_session_ids = kwargs["tools"][0].params_json_schema["properties"][
            "session_id"
        ]["enum"]
        if self.calls == 1:
            output = [
                ResponseFunctionToolCall(
                    id="fc_test",
                    call_id="call_test",
                    type="function_call",
                    name="run_python_in_sandbox",
                    arguments=json.dumps(
                        {
                            "session_id": self.session_id,
                            "prompt": "Write Python to compute 6 * 7 and run it",
                        }
                    ),
                )
            ]
        else:
            assert "42" in str(kwargs.get("input", args))
            output = [
                ResponseOutputMessage(
                    id="msg_test",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(type="output_text", text="Result: 42", annotations=[])
                    ],
                )
            ]
        return ModelResponse(output=output, usage=Usage(requests=1), response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield


@pytest.mark.asyncio
async def test_real_agents_sdk_tool_loop_and_history(tmp_path):
    set_tracing_disabled(True)

    class Manager:
        def sessions(self, agent_id):
            return [{"id": "session-a", "status": "ready", "sandbox_id": "one"}]

        async def prompt(self, agent_id, session_id, prompt):
            assert (agent_id, session_id) == ("agent-a", "session-a")
            assert prompt.startswith("Execute this task now")
            return {"output": "42", "tool_results": [{"tool": "bash", "result": "42"}]}

    model = ScriptedModel("session-a")
    result = await run_agent(
        Manager(),
        "agent-a",
        "Compute 6 * 7",
        model=model,
        history_path=str(tmp_path / "history.db"),
    )
    assert model.allowed_session_ids == ["session-a"]
    assert result["output"] == "Result: 42"
    assert result["sandbox_results"][0]["output"] == "42"
    assert result["usage"]["requests"] == 2
    assert (tmp_path / "history.db").exists()


@pytest.mark.asyncio
async def test_reject_foreign_tool_session(tmp_path):
    set_tracing_disabled(True)

    class Manager:
        def sessions(self, agent_id):
            return [{"id": "allowed", "status": "ready"}]

        async def prompt(self, *args):
            pytest.fail("Unauthorized session must not reach the worker")

    with pytest.raises(Exception, match="Session is not authorized"):
        await run_agent(
            Manager(),
            "a",
            "compute",
            model=ScriptedModel("foreign"),
            history_path=str(tmp_path / "h.db"),
        )
