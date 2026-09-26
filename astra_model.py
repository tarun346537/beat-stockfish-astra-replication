"""Direct Responses adapter for the unchanged, pinned Inspect rollout.

Inspect 0.3.260 predates GPT-6 capability detection. Its supported ModelInfo
family override supplies modern Responses serialization without changing the
wire model. This module never adds prompts, tools, reasoning effort, or an
output-token cap. Run this file directly for an entirely offline transport test.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from inspect_ai.model import (
    GenerateConfig,
    Model,
    ModelInfo,
    get_model,
    set_model_info,
)

MODEL = "openai/gpt-6-astra"
WIRE_MODEL = "gpt-6-astra"
BASE_URL = "https://api.openai.com/v1"
MAX_POSSIBLE_OUTPUT_TOKENS = 128_000
CONTEXT_TOKENS = 1_050_000


def build_model(api_key: str, http_client: httpx.AsyncClient) -> Model:
    """Build one fresh model; the supplied guarded client must remain open.

    A cost guard must reserve for 128,000 output tokens when max_output_tokens
    is absent. Raising Inspect's LimitExceededError from the client preserves
    the shipped runner's normal limit handling and subsequent grading.
    """
    if http_client.is_closed:
        raise RuntimeError("Refusing a closed guarded HTTP client")
    set_model_info(
        MODEL,
        ModelInfo(
            organization="OpenAI",
            model=WIRE_MODEL,
            family="gpt-5.6",
            context_length=CONTEXT_TOKENS,
            output_tokens=MAX_POSSIBLE_OUTPUT_TOKENS,
            reasoning=True,
        ),
    )
    config = GenerateConfig(
        max_tokens=None,
        reasoning_effort=None,
        reasoning_summary="auto",
        max_retries=0,
        max_connections=1,
        cache=False,
    )
    model = get_model(
        MODEL,
        config=config,
        base_url=BASE_URL,
        api_key=api_key,
        memoize=False,
        responses_api=True,
        responses_store=False,
        background=False,
        service_tier="default",
        max_retries=0,
        http_client=http_client,
    )
    api = model.api
    original_initialize = api.initialize

    def guarded_initialize() -> None:
        # Native initialize replaces a closed client with an unguarded default.
        # Fail before that path, including when Inspect reinitializes providers.
        if api.http_client is not http_client or http_client.is_closed:
            raise RuntimeError("Guarded HTTP client was replaced or closed")
        original_initialize()
        if api.http_client is not http_client or api.client._client is not http_client:
            raise RuntimeError("Native provider did not retain guarded HTTP client")

    api.initialize = guarded_initialize
    if api.http_client is not http_client or api.client._client is not http_client:
        raise RuntimeError("Native provider did not retain guarded HTTP client")
    return model


async def offline_transport_test(transport_wrapper=None) -> dict[str, Any]:
    """Exercise native request/response conversion with no network access."""
    from inspect_ai.model import ChatMessageTool, ChatMessageUser
    from inspect_ai.tool import Tool, tool

    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BASE_URL + "/responses/input_tokens":
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 10})
        assert str(request.url) == BASE_URL + "/responses"
        assert request.method == "POST"
        body = json.loads(request.content)
        requests.append(body)
        assert len(requests) <= 2, "Unexpected capability probe or retry"
        if len(requests) == 1:
            output = [
                {
                    "id": "rs_test_1",
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": "offline-encrypted-reasoning",
                },
                {
                    "id": "fc_test_1",
                    "type": "function_call",
                    "call_id": "call_test_1",
                    "name": "capped_bash",
                    "arguments": '{"cmd":"printf mock"}',
                    "status": "completed",
                },
            ]
        else:
            output = [
                {
                    "id": "msg_test_2",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "done", "annotations": []}
                    ],
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": f"resp_test_{len(requests)}",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": WIRE_MODEL,
                "output": output,
                "error": None,
                "incomplete_details": None,
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 2},
                    "total_tokens": 15,
                },
            },
        )

    @tool
    def capped_bash() -> Tool:
        async def execute(cmd: str) -> str:
            """Offline test tool.

            Args:
                cmd: A command that is never executed in this test.
            """
            raise AssertionError("Offline test must not execute tools")

        return execute

    transport = httpx.MockTransport(handle)
    if transport_wrapper is not None:
        transport = transport_wrapper(transport)
    client = httpx.AsyncClient(transport=transport)
    model = build_model("offline-placeholder-not-a-credential", client)
    user = ChatMessageUser(content="offline fixture")
    try:
        assert model.api.client.max_retries == 0
        assert model.config.max_retries == 0
        first = await model.generate([user], tools=[capped_bash()])
        result = ChatMessageTool(
            content="mock", tool_call_id="call_test_1", function="capped_bash"
        )
        await model.generate([user, first.message, result], tools=[capped_bash()])
        assert len(requests) == 2
        for body in requests:
            assert body["model"] == WIRE_MODEL
            assert body["store"] is False
            assert body["background"] is False
            assert body["service_tier"] == "default"
            assert body["reasoning"] == {"summary": "auto"}
            assert "reasoning.encrypted_content" in body["include"]
            assert "access_programs" not in body
            assert "max_output_tokens" not in body
            assert "previous_response_id" not in body
            assert "instructions" not in body
        assert any(
            item.get("encrypted_content") == "offline-encrypted-reasoning"
            for item in requests[1]["input"]
        )
        assert any(
            item.get("type") == "function_call_output"
            and item.get("call_id") == "call_test_1"
            and item.get("output") == "mock"
            for item in requests[1]["input"]
        )
    finally:
        await client.aclose()
    try:
        model.api.initialize()
    except RuntimeError:
        pass
    else:
        raise AssertionError("Closed client must never be replaced")
    assert model.api.http_client is client
    return {
        "passed": True,
        "network_calls": 0,
        "mock_generation_requests": len(requests),
        "wire_model": WIRE_MODEL,
        "capability_family": "gpt-5.6",
        "reasoning": requests[0]["reasoning"],
        "output_cap_omitted": True,
        "encrypted_history_replayed": True,
        "function_result_replayed": True,
        "closed_guard_bypass_blocked": True,
        "request_keys": sorted(requests[0]),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(offline_transport_test()), indent=2))
