"""Configure a reserved Pi provider from environment; never write a literal API key."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def configure(agent_dir, env):
    base_url = env.get("PI_BASE_URL", "").strip()
    mode = env.get("PI_API_MODE", "").strip()
    # Preserve the built-in OpenAI model catalog when custom transport is not requested.
    if not base_url and not mode:
        return "openai"
    base_url = base_url or "https://api.openai.com/v1"
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("PI_BASE_URL must be an HTTP(S) base URL without credentials, query or fragment")
    mode = mode or "chat_completions"
    apis = {"chat_completions": "openai-completions", "responses": "openai-responses"}
    if mode not in apis:
        raise ValueError("PI_API_MODE must be chat_completions or responses")
    context = int(env.get("PI_CONTEXT_WINDOW") or "128000")
    output = int(env.get("PI_MAX_TOKENS") or "4096")
    if not 0 < output <= context:
        raise ValueError("Require 0 < PI_MAX_TOKENS <= PI_CONTEXT_WINDOW")
    compat = json.loads(env.get("PI_MODEL_COMPAT") or "{}")
    if not isinstance(compat, dict):
        raise ValueError("PI_MODEL_COMPAT must be a JSON object")
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "models.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    provider = {
        "baseUrl": base_url, "api": apis[mode], "apiKey": "$PI_API_KEY", "authHeader": True,
        "models": [{"id": env.get("PI_MODEL") or "gpt-4.1-mini", "reasoning": False,
                    "input": ["text"], "contextWindow": context, "maxTokens": output}],
    }
    if mode == "chat_completions":
        provider["compat"] = {"supportsStore": False, "supportsDeveloperRole": False,
                              "supportsReasoningEffort": False, "maxTokensField": "max_tokens", **compat}
    elif compat:
        provider["compat"] = compat
    data.setdefault("providers", {})["sandbox-openai"] = provider
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
    return "sandbox-openai"


if __name__ == "__main__":
    print(configure(Path("/workspace/home/.pi/agent"), os.environ))
