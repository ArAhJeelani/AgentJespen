import os
from importlib.metadata import version

from google import genai
from google.genai import errors, types

from agent.provider import Call, ProviderError, Result, Turn


_API_MESSAGES = {
    400: "Gemini rejected the request parameters or conversation format.",
    401: "Gemini could not authenticate the request.",
    403: "Gemini denied access to the requested resource.",
    404: "Gemini could not find the requested model or resource.",
    408: "Gemini reported a request timeout.",
    429: "Gemini reported rate or quota exhaustion.",
    500: "Gemini reported an internal error.",
    503: "Gemini reported that the service is unavailable.",
    504: "Gemini reported a gateway timeout.",
}


def thinking_from_env():
    budget = os.getenv("GEMINI_THINKING_BUDGET")
    return {
        "thinking_level": os.getenv("GEMINI_THINKING_LEVEL"),
        "thinking_budget": int(budget) if budget is not None else None,
    }


class Gemini:
    def __init__(self, *, api_key: str, model: str, temperature: float | None = None,
                 top_p: float | None = None, top_k: int | None = None, seed: int | None = None,
                 thinking_level: str | None = None, thinking_budget: int | None = None):
        if not api_key or not model:
            raise ValueError("Set GEMINI_API_KEY and GEMINI_MODEL")
        if thinking_level is not None:
            thinking_level = thinking_level.upper()
            if thinking_level not in {"MINIMAL", "LOW", "MEDIUM", "HIGH"}:
                raise ValueError("Unrecognized Gemini thinking level")
        if thinking_level is not None and thinking_budget is not None:
            raise ValueError("Set only one Gemini thinking control")
        if thinking_budget is not None and thinking_budget < -1:
            raise ValueError("Gemini thinking budget must be at least -1")
        thinking = {"thinking_level": thinking_level, "thinking_budget": thinking_budget}
        self.thinking_config = types.ThinkingConfig(**thinking) if any(
            value is not None for value in thinking.values()
        ) else None
        self.client = genai.Client(
            api_key=api_key, vertexai=False,
            http_options=types.HttpOptions(
                base_url="https://generativelanguage.googleapis.com",
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        ).aio
        self.model = model
        self.sampling = {"temperature": temperature, "top_p": top_p, "top_k": top_k, "seed": seed}
        self.configuration = {
            "provider": "gemini", "model": model, "sdk": "google-genai",
            "sdk_version": version("google-genai"), "sampling": self.sampling.copy(),
            "thinking": thinking, "sdk_attempts": 1, "automatic_continuation": False,
        }
        self.contents: list[types.Content] = []

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.client.__aexit__(*exc)

    async def generate(self, *, instruction: str, system_prompt: str, tools: list[dict],
                       results: list[Result], max_output_tokens: int) -> Turn:
        if not self.contents:
            self.contents.append(types.Content(role="user", parts=[types.Part(text=instruction)]))
        elif results:
            self.contents.append(types.Content(role="user", parts=[
                types.Part(function_response=types.FunctionResponse(
                    id=result.call.provider_id, name=result.call.name, response=result.payload
                )) for result in results
            ]))
        declarations = [
            types.FunctionDeclaration(name=tool["name"], description=tool["description"],
                                      parameters_json_schema=tool["parameters"])
            for tool in tools
        ]
        # Older SDKs did not implement automatic continuation.
        continuation = ({"automatic_continuation": False}
                        if "automatic_continuation" in types.GenerateContentConfig.model_fields else {})
        try:
            response = await self.client.models.generate_content(
                model=self.model,
                contents=list(self.contents),
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=[types.Tool(function_declarations=declarations)],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    max_output_tokens=max_output_tokens,
                    thinking_config=self.thinking_config,
                    **self.sampling,
                    **continuation,
                ),
            )
        except errors.APIError as exc:
            # Remote messages and details can echo prompts, URLs, or credentials.
            code = exc.code if isinstance(exc.code, int) else "gemini_api_error"
            message = _API_MESSAGES.get(code, "Gemini returned an API error.")
            if code == 400 and any(
                hint in (exc.message or "").lower() for hint in ("thought signature", "thought_signature")
            ):
                message = "Gemini rejected the conversation's thought signature."
            raise ProviderError(code, message, type(exc).__name__) from None
        if not response.candidates or response.candidates[0].content is None:
            raise ProviderError("empty_candidate", "Gemini returned no candidate content.", "ResponseError")
        candidate = response.candidates[0]
        # Preserve the original content parts, including opaque thought signatures.
        self.contents.append(candidate.content)
        parts = candidate.content.parts or []
        usage = response.usage_metadata
        counts = {
            "input_tokens": usage.prompt_token_count,
            "output_tokens": usage.candidates_token_count,
            "thought_tokens": usage.thoughts_token_count,
            "total_tokens": usage.total_token_count,
        } if usage is not None else {}
        return Turn(
            text="".join(part.text for part in parts if part.text and not part.thought),
            calls=tuple(Call(part.function_call.name or "", part.function_call.args or {},
                             part.function_call.id)
                        for part in parts if part.function_call is not None),
            usage={key: count for key, count in counts.items() if count is not None},
            model_version=response.model_version,
            response_id=response.response_id,
            finish_reason=str(candidate.finish_reason.value) if candidate.finish_reason else None,
        )
