"""Provider-neutral tool turns. A provider instance belongs to one execution."""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Call:
    name: str
    arguments: dict
    provider_id: str | None = None


@dataclass(frozen=True)
class Turn:
    text: str = ""
    calls: tuple[Call, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    model_version: str | None = None
    finish_reason: str | None = None
    response_id: str | None = None


@dataclass(frozen=True)
class Result:
    invocation_id: str
    call: Call
    payload: dict


class ProviderError(Exception):
    """An adapter-classified error with diagnostic fields safe for history."""

    def __init__(self, code: int | str, message: str, error_type: str):
        super().__init__(message)
        self.code = code
        self.error_type = error_type


class Provider(Protocol):
    configuration: dict

    async def generate(
        self, *, instruction: str, system_prompt: str, tools: list[dict],
        results: list[Result], max_output_tokens: int,
    ) -> Turn: ...
