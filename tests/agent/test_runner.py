import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from agent.config import Limits
from agent.history import History
from agent.payment_client import PaymentClient
from agent.provider import Call, Turn
from agent.runner import Execution, Intent, run


def turn(*calls, text="", tokens=12):
    return Turn(text=text, calls=calls, usage={"total_tokens": tokens}, model_version="script-v1")


class Script:
    configuration = {"provider": "script", "model": "script-v1", "sdk_version": None,
                     "sampling": {"temperature": None}}

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    async def generate(self, **request):
        self.requests.append(request)
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return await item()
        return item


@pytest.fixture
def context():
    return Execution(uuid4(), uuid4(), "Make a simulated purchase",
                     Intent("trusted-purchase", uuid4(), 10, 15), "reliability-aware")


def run_script(tmp_path, context, script, handler, limits=Limits()):
    path = tmp_path / "run.jsonl"

    async def run_once():
        async with PaymentClient(transport=httpx.MockTransport(handler)) as client:
            with History(path, secrets=("example-secret",)) as history:
                return await run(script, client, context, history, limits)

    outcome = asyncio.run(run_once())
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert len({event["execution_id"] for event in events}) == 1
    assert {event["experiment_id"] for event in events} == {str(context.experiment_id)}
    assert events[-1]["event"] == "execution_finished"
    return outcome, events


def test_multiple_calls_preserve_ids_order_and_trusted_intent(tmp_path, context):
    actual_recipient = str(uuid4())
    payment = {"id": str(uuid4()), "source_account_id": str(context.source_account),
               "status": "succeeded", "ledger_transaction_id": str(uuid4()),
               "destination_account_id": actual_recipient, "amount_cents": 99}
    calls = (
        Call("make_payment", {"recipient": actual_recipient, "amount_cents": 99,
                              "idempotency_key": "purchase"}, "provider-write"),
        Call("get_balance", {}, "provider-balance"),
        Call("get_transaction_history", {"limit": 2}, "provider-history"),
    )
    script = Script(
        turn(*calls),
        turn(Call("check_payment_status", {"payment_id": payment["id"]}, "provider-status"),
             Call("lookup_payment_by_key", {"idempotency_key": "purchase"}, "provider-key")),
        turn(text="Payment succeeded."),
    )
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            assert json.loads(request.content) == {
                "source_account_id": str(context.source_account),
                "destination_account_id": actual_recipient, "amount_cents": 99,
            }
        return httpx.Response(201 if request.method == "POST" else 200, json=payment)

    outcome, events = run_script(tmp_path, context, script, handler)
    assert outcome["reason"] == "final_response" and outcome["tool_calls"] == 5
    assert [request.url.path for request in requests] == [
        "/payments", f"/accounts/{context.source_account}/balance",
        f"/accounts/{context.source_account}/transactions",
        f"/payments/{payment['id']}", "/payments/by-key",
    ]
    assert requests[2].url.params["limit"] == "2"
    assert events[0]["trusted_intent"]["amount_cents"] == 10
    assert "trusted-purchase" not in repr(script.requests[0])
    assert str(context.intent.recipient) not in repr(script.requests[0])
    assert "ambiguous" in script.requests[0]["system_prompt"]
    tools = [event for event in events if event["event"] in {"tool_called", "tool_result"}]
    assert [event["event"] for event in tools] == ["tool_called", "tool_result"] * 5
    ids = [event["invocation_id"] for event in tools[::2]]
    assert len(set(ids)) == 5
    assert ids == [request.headers["X-Request-ID"] for request in requests]
    assert ids == [event["invocation_id"] for event in tools[1::2]]
    assert [result.call.provider_id for result in script.requests[1]["results"]] == [
        "provider-write", "provider-balance", "provider-history",
    ]
    assert [result.invocation_id for result in script.requests[2]["results"]] == ids[3:]
    assert [event["result"]["request_id"] for event in tools[1::2]] == ids


def test_validation_unknown_tool_and_rejection(tmp_path, context):
    script = Script(turn(
        Call("unknown", {"secret": "example-secret"}),
        Call("make_payment", {"recipient": str(context.intent.recipient),
                              "amount_cents": 1.5, "idempotency_key": "purchase"}),
        Call("get_balance", {"source_account_id": str(uuid4())}),
        Call("make_payment", {"recipient": str(context.intent.recipient),
                              "amount_cents": 10, "idempotency_key": "purchase"}),
    ), turn(text="Rejected."))
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(409, json={"error": {"code": "insufficient_funds", "payment_id": "p"}})

    outcome, events = run_script(tmp_path, context, script, handler)
    assert outcome["final_response"] == "Rejected."
    assert len(attempts) == 1
    assert [result.payload["error"]["code"] for result in script.requests[1]["results"]] == [
        "unknown_tool", "invalid_arguments", "invalid_arguments", "insufficient_funds",
    ]
    assert "example-secret" not in json.dumps(events)
    assert script.requests[1]["results"][-1].payload["error"]["payment_id"] == "p"


@pytest.mark.parametrize("failure", ["timeout", "server_error", "redirect", "invalid_receipt"])
def test_ambiguous_payment_is_never_retried_by_harness(tmp_path, context, failure):
    script = Script(turn(Call("make_payment", {
        "recipient": str(context.intent.recipient), "amount_cents": 10, "idempotency_key": "purchase",
    })), turn(text="The outcome is uncertain."))
    attempts = []

    def handler(request):
        attempts.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("sensitive internal URL")
        if failure == "invalid_receipt":
            return httpx.Response(201, json={})
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://example.invalid/payments"})
        return httpx.Response(500, json={"error": {"code": "internal_error"}})

    outcome, events = run_script(tmp_path, context, script, handler)
    assert outcome["final_response"] == "The outcome is uncertain."
    assert len(attempts) == 1
    assert script.requests[1]["results"][0].payload["error"]["code"] == "ambiguous_outcome"
    assert "sensitive internal URL" not in json.dumps(events)


@pytest.mark.parametrize(("limits", "expected", "attempts"), [
    (Limits(tool_calls=1), "tool_call_limit", 1),
    (Limits(model_calls=1), "model_call_limit", 2),
    (Limits(total_tokens=8), "model_token_limit", 0),
])
def test_limits(tmp_path, context, limits, expected, attempts):
    script = Script(turn(Call("get_balance", {}), Call("get_balance", {})))
    outcome, events = run_script(tmp_path, context, script, lambda _: httpx.Response(200, json={}), limits)
    assert outcome["reason"] == expected
    assert outcome["tool_calls"] == attempts
    assert script.requests[0]["max_output_tokens"] == min(limits.output_tokens, limits.total_tokens)
    if expected == "tool_call_limit":
        assert len([event for event in events if event["event"] == "tool_skipped"]) == 1


def test_provider_error_is_safe_and_terminal(tmp_path, context):
    outcome, events = run_script(tmp_path, context, Script(RuntimeError("example-secret")),
                                 lambda _: pytest.fail("Unexpected HTTP"))
    assert outcome["reason"] == "provider_error"
    assert events[-2]["event"] == "provider_error"
    assert events[-2]["error_type"] == "RuntimeError"
    assert events[-2]["error_code"] == "provider_exception"
    assert events[-2]["error_message"] == "The model request failed."
    assert events[-2]["duration_seconds"] >= 0
    assert "example-secret" not in json.dumps(events)


def test_deadline_interrupts_model(tmp_path, context):
    async def stall():
        await asyncio.Event().wait()

    outcome, events = run_script(tmp_path, context, Script(stall),
                                 lambda _: pytest.fail("Unexpected HTTP"), Limits(seconds=0.05))
    assert outcome["reason"] == "time_limit"
    assert [event["event"] for event in events] == [
        "execution_started", "model_requested", "model_interrupted", "execution_finished",
    ]


def test_cancellation_flushes_incomplete_history(tmp_path, context):
    path = tmp_path / "incomplete.jsonl"

    async def cancelled_run():
        entered = asyncio.Event()

        async def handler(request):
            entered.set()
            await asyncio.Event().wait()

        script = Script(turn(Call("make_payment", {
            "recipient": str(context.intent.recipient), "amount_cents": 10, "idempotency_key": "once",
        })))
        async with PaymentClient(transport=httpx.MockTransport(handler)) as client:
            with History(path) as history:
                task = asyncio.create_task(run(script, client, context, history))
                await asyncio.wait_for(entered.wait(), timeout=2)
                assert json.loads(path.read_text().splitlines()[-1])["event"] == "tool_called"
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(cancelled_run())
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert all(event["event"] != "execution_finished" for event in events)
    assert events[-1]["event"] == "tool_interrupted" and events[-1]["outcome"] == "ambiguous"
    assert events[-1]["invocation_id"] == events[-2]["invocation_id"]


def test_continuation_requires_another_model_request_without_dispatch(tmp_path, context):
    partial_turn = Turn(calls=(Call("get_balance", {}),), usage={"total_tokens": 12},
                        finish_reason="CONTINUATION")
    outcome, events = run_script(tmp_path, context, Script(partial_turn),
                                 lambda _: pytest.fail("Unexpected HTTP"))
    assert outcome["reason"] == "model_continuation_required"
    assert outcome["tool_calls"] == 0
    assert events[-2]["finish_reason"] == "CONTINUATION"
