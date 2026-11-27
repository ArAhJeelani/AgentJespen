import asyncio
from dataclasses import asdict, dataclass
from time import monotonic
from uuid import UUID, uuid4

from agent.config import Limits, PROMPTS, PROMPT_VERSION
from agent.history import History
from agent.payment_client import PaymentClient
from agent.provider import Provider, ProviderError, Result
from agent.tools import definitions, execute


@dataclass(frozen=True)
class Intent:
    purchase_id: str
    recipient: UUID
    amount_cents: int
    budget_cents: int


@dataclass(frozen=True)
class Execution:
    experiment_id: UUID
    source_account: UUID
    instruction: str
    intent: Intent
    configuration: str = "baseline"


async def run(provider: Provider, client: PaymentClient, execution: Execution,
              history: History, limits: Limits = Limits()) -> dict:
    execution_id = str(uuid4())
    identifiers = {"experiment_id": str(execution.experiment_id), "execution_id": execution_id}

    def record(event, **fields):
        history.write(event, **identifiers, **fields)

    prompt = PROMPTS[execution.configuration]
    start = monotonic()
    record("execution_started", source_account=str(execution.source_account),
           agent_visible={"instruction": execution.instruction, "system_prompt": prompt},
           trusted_intent=asdict(execution.intent) | {"recipient": str(execution.intent.recipient)},
           prompt_configuration=execution.configuration, prompt_version=PROMPT_VERSION,
           model=provider.configuration, limits=asdict(limits))
    model_calls = tool_calls = tokens = 0
    final = None
    reason = "model_call_limit"
    results: list[Result] = []
    tool_definitions = definitions()
    try:
        async with asyncio.timeout(limits.seconds):
            while model_calls < limits.model_calls:
                model_calls += 1
                max_output = min(limits.output_tokens, limits.total_tokens - tokens)
                record("model_requested", model_call=model_calls, max_output_tokens=max_output,
                       result_invocation_ids=[result.invocation_id for result in results])
                call_started = monotonic()
                try:
                    turn = await provider.generate(
                        instruction=execution.instruction, system_prompt=prompt, tools=tool_definitions,
                        results=results, max_output_tokens=max_output,
                    )
                except asyncio.CancelledError:
                    record("model_interrupted", model_call=model_calls,
                           duration_seconds=monotonic() - call_started)
                    raise
                except Exception as exc:
                    classified = isinstance(exc, ProviderError)
                    record("provider_error", model_call=model_calls,
                           duration_seconds=monotonic() - call_started,
                           error_type=exc.error_type if classified else type(exc).__name__,
                           error_code=exc.code if classified else "provider_exception",
                           error_message=str(exc) if classified else "The model request failed.")
                    reason = "provider_error"
                    break

                invocations = [(str(uuid4()), call) for call in turn.calls]
                record("model_responded", model_call=model_calls, text=turn.text, usage=turn.usage,
                       duration_seconds=monotonic() - call_started, response_id=turn.response_id,
                       model_version=turn.model_version, finish_reason=turn.finish_reason,
                       calls=[{"invocation_id": uid, **asdict(call)} for uid, call in invocations])
                used = turn.usage.get("total_tokens")
                if not isinstance(used, int) or used < 0:
                    reason = "usage_unavailable"
                    break
                tokens += used
                if tokens > limits.total_tokens or (tokens == limits.total_tokens and invocations):
                    reason = "model_token_limit"
                    break
                if turn.finish_reason == "CONTINUATION":
                    reason = "model_continuation_required"
                    break
                if not invocations:
                    final = turn.text or None
                    reason = "final_response" if final else "empty_model_response"
                    if turn.finish_reason == "MAX_TOKENS":
                        reason = "model_output_limit"
                    break

                results = []
                for uid, call in invocations:
                    fields = {"invocation_id": uid, "provider_id": call.provider_id, "name": call.name}
                    if tool_calls >= limits.tool_calls:
                        record("tool_skipped", **fields, reason="tool_call_limit")
                        continue
                    tool_calls += 1
                    record("tool_called", **fields, arguments=call.arguments)
                    try:
                        payload = await execute(client, execution.source_account, call, uid)
                    except asyncio.CancelledError:
                        record("tool_interrupted", **fields, request_id=uid,
                               outcome="ambiguous" if call.name == "make_payment" else "unknown")
                        raise
                    record("tool_result", **fields, result=payload)
                    results.append(Result(uid, call, payload))
                if len(results) != len(invocations):
                    reason = "tool_call_limit"
                    break
    except TimeoutError:
        reason = "time_limit"

    outcome = {"reason": reason, "final_response": final, "duration_seconds": monotonic() - start,
               "model_calls": model_calls, "tool_calls": tool_calls, "total_tokens": tokens}
    record("execution_finished", **outcome)
    return outcome
