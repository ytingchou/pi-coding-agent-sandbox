"""Explicit client selection for OpenAI or an internal compatible gateway."""

import os
from urllib.parse import urlsplit

from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel
from openai import AsyncOpenAI


def configured_model():
    base_url = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "OPENAI_BASE_URL must be an HTTP(S) base URL without credentials, query or fragment"
        )
    mode = os.getenv("OPENAI_API_MODE") or (
        "chat_completions" if os.getenv("OPENAI_BASE_URL") else "responses"
    )
    adapters = {"responses": OpenAIResponsesModel, "chat_completions": OpenAIChatCompletionsModel}
    if mode not in adapters:
        raise ValueError("OPENAI_API_MODE must be chat_completions or responses")
    client = AsyncOpenAI(base_url=base_url, api_key=os.getenv("OPENAI_API_KEY"), max_retries=0)
    return adapters[mode](
        model=os.getenv("OPENAI_MODEL") or "gpt-4.1-mini", openai_client=client
    ), client
