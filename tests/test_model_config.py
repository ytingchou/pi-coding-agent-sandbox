import json

import pytest
from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel

from orchestrator.model_config import configured_model
from sandbox.isolation import environment, redact
from sandbox.resources.configure_model import configure


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,adapter",
    [("chat_completions", OpenAIChatCompletionsModel), ("responses", OpenAIResponsesModel)],
)
async def test_explicit_outer_transport(monkeypatch, mode, adapter):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gateway.internal:8000/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "outer-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "company-model")
    monkeypatch.setenv("OPENAI_API_MODE", mode)
    model, client = configured_model()
    try:
        assert isinstance(model, adapter)
        assert model.model == "company-model"
        assert str(client.base_url) == "http://gateway.internal:8000/v1/"
        assert client.api_key == "outer-test-key"
    finally:
        await client.close()


def test_pi_custom_models_and_secret_reference(tmp_path, monkeypatch):
    p = tmp_path / "models.json"
    p.write_text(json.dumps({"providers": {"other": {"models": []}}}))
    env = {
        "PI_BASE_URL": "http://gateway.internal/v1",
        "PI_MODEL": "arbitrary-company-model",
        "PI_API_KEY": "do-not-store-this-key",
        "PI_API_MODE": "chat_completions",
    }
    assert configure(tmp_path, env) == "sandbox-openai"
    data = json.loads(p.read_text())
    assert "other" in data["providers"]
    provider = data["providers"]["sandbox-openai"]
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "$PI_API_KEY"
    assert env["PI_API_KEY"] not in p.read_text()
    env["PI_API_MODE"] = "responses"
    configure(tmp_path, env)
    assert json.loads(p.read_text())["providers"]["sandbox-openai"]["api"] == "openai-responses"
    assert configure(tmp_path, {}) == "openai"
    monkeypatch.setenv("OPENAI_API_KEY", "outer-key")
    monkeypatch.setenv("PI_API_KEY", "pi-key")
    assert environment()["OPENAI_API_KEY"] == "pi-key"
    assert environment()["PI_API_KEY"] == "pi-key"
    assert redact("outer-key pi-key") == "[redacted] [redacted]"


def test_invalid_modes_fail_before_network(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_MODE", "typo")
    with pytest.raises(ValueError, match="OPENAI_API_MODE"):
        configured_model()
    with pytest.raises(ValueError, match="PI_API_MODE"):
        configure(tmp_path, {"PI_BASE_URL": "http://internal/v1", "PI_API_MODE": "typo"})
