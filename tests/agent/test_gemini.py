import asyncio
import json

import httpx
import pytest

from agent.provider import Result
from agent.tools import definitions


def test_gemini_wire_preserves_calls_and_signatures(monkeypatch):
    pytest.importorskip("google.genai")
    from agent.providers import gemini

    payloads = []
    original_client = gemini.genai.Client

    def handle(request):
        assert request.url.host == "generativelanguage.googleapis.com"
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            parts = [
                {"functionCall": {"name": "get_balance", "args": {}, "id": "call-1"},
                 "thoughtSignature": "YWJj"},
                {"functionCall": {"name": "get_balance", "args": {}, "id": "call-2"}},
            ]
        else:
            parts = [{"text": "Done."}]
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
            "modelVersion": "example-version", "responseId": "example-response-id",
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 8, "totalTokenCount": 120},
        })

    def mock_client(**kwargs):
        kwargs["http_options"].httpx_async_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle)
        )
        return original_client(**kwargs)

    monkeypatch.setattr(gemini.genai, "Client", mock_client)

    async def check():
        async with gemini.Gemini(api_key="fake-key", model="test-model", temperature=0.2,
                                 top_p=0.8, top_k=5, seed=42, thinking_level="low") as provider:
            inputs = {"instruction": "Read balance", "system_prompt": "System prompt",
                      "tools": definitions(), "max_output_tokens": 256}
            first = await provider.generate(**inputs, results=[])
            assert provider.configuration["sdk_version"]
            assert first.response_id == "example-response-id"
            assert first.usage["thought_tokens"] == 8
            assert first.usage["total_tokens"] == 120 and first.model_version == "example-version"
            assert [call.provider_id for call in first.calls] == ["call-1", "call-2"]
            results = [Result(str(i), call, {"balance": i}) for i, call in enumerate(first.calls)]
            assert (await provider.generate(**inputs, results=results)).text == "Done."

    asyncio.run(check())
    assert payloads[0]["generationConfig"] == {
        "temperature": 0.2, "topP": 0.8, "topK": 5, "seed": 42, "maxOutputTokens": 256,
        "thinkingConfig": {"thinking_level": "LOW"},
    }
    assert len(payloads[0]["tools"][0]["functionDeclarations"]) == 5
    content = payloads[1]["contents"]
    assert content[1]["parts"][0]["thoughtSignature"] == "YWJj"
    assert [part["functionResponse"] for part in content[2]["parts"]] == [
        {"id": "call-1", "name": "get_balance", "response": {"balance": 0}},
        {"id": "call-2", "name": "get_balance", "response": {"balance": 1}},
    ]


@pytest.mark.parametrize("provider_outcome", ["retryable_error", "continuation"])
def test_gemini_makes_one_request_and_classifies_remote_errors(monkeypatch, provider_outcome):
    pytest.importorskip("google.genai")
    from agent.provider import ProviderError
    from agent.providers import gemini

    requests = []
    original_client = gemini.genai.Client

    def handle(request):
        requests.append(request)
        if provider_outcome == "retryable_error":
            return httpx.Response(503, json={
                "error": {"status": "UNAVAILABLE", "message": "echoed-secret"},
            })
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": "Partial"}]},
                            "finishReason": "CONTINUATION"}],
            "continuationToken": "YWJj",
            "usageMetadata": {"totalTokenCount": 8},
        })

    def mock_client(**kwargs):
        kwargs["http_options"].httpx_async_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle)
        )
        return original_client(**kwargs)

    monkeypatch.setattr(gemini.genai, "Client", mock_client)

    async def check():
        async with gemini.Gemini(api_key="fake-key", model="test-model") as provider:
            args = dict(instruction="Test", system_prompt="Test", tools=definitions(),
                        results=[], max_output_tokens=256)
            if provider_outcome == "retryable_error":
                with pytest.raises(ProviderError) as exc:
                    await provider.generate(**args)
                assert exc.value.code == 503
                assert str(exc.value) == "Gemini reported that the service is unavailable."
            else:
                assert (await provider.generate(**args)).finish_reason == "CONTINUATION"

    asyncio.run(check())
    assert len(requests) == 1
