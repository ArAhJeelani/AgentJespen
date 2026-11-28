import asyncio
import json
import os
import socket
from pathlib import Path
from threading import Event, Thread
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import text

from agent.config import Limits
from agent.faults import FaultPlan, FaultRule
from agent.history import History
from agent.payment_client import PaymentClient
from agent.provider import Call, Turn
from agent.runner import Execution, Intent, run
from app.main import create_app


@pytest.fixture
def simulator(engine):
    ready = Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            ready.set()

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = Server(uvicorn.Config(create_app(engine), access_log=False, log_level="warning",
                                       loop="asyncio", http="h11", ws="none"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            assert ready.wait(5) and server.started
            yield url
        finally:
            server.should_exit = True
            thread.join(5)
            assert not thread.is_alive()


def prepare(simulator):
    ids = []
    for amount in (100, 0):
        response = httpx.post(f"{simulator}/accounts", json={"opening_balance_cents": amount}, trust_env=False)
        assert response.status_code == 201
        ids.append(UUID(response.json()["id"]))
    return ids


def context_for(source, recipient, configuration="baseline"):
    return Execution(
        uuid4(), source,
        f"Make one simulated payment of 25 cents to {recipient} with idempotency key smoke-purchase. "
        "Then report the outcome.",
        Intent("smoke-purchase", recipient, 25, 25), configuration,
    )


def execute(simulator, context, provider, history_path: Path, key=""):
    async def execute_once():
        async with PaymentClient(simulator) as client:
            with History(history_path, secrets=(key,)) as history:
                return await run(provider, client, context, history, Limits(seconds=90))

    return asyncio.run(execute_once())


def verify(simulator, source, recipient):
    with httpx.Client(base_url=simulator, trust_env=False) as client:
        response = client.get("/payments/by-key", headers={"Idempotency-Key": "smoke-purchase"})
        assert response.status_code == 200, response.text
        payment = response.json()
        assert payment["status"] == "succeeded" and payment["amount_cents"] == 25
        assert payment["source_account_id"] == str(source)
        assert payment["destination_account_id"] == str(recipient)
        assert payment["ledger_transaction_id"]
        assert client.get(f"/accounts/{source}/balance").json()["balance_cents"] == 75
        assert client.get(f"/accounts/{recipient}/balance").json()["balance_cents"] == 25
    return payment


def test_scripted_agent_over_real_http(simulator, tmp_path):
    source, recipient = prepare(simulator)

    class Script:
        configuration = {"provider": "script", "model": "script-v1", "sdk_version": None,
                         "sampling": {"temperature": None}}

        async def generate(self, *, results, **kwargs):
            if not results:
                return Turn(calls=(Call("make_payment", {
                    "recipient": str(recipient), "amount_cents": 25,
                    "idempotency_key": "smoke-purchase",
                }, "script-write"), Call("get_balance", {}, "script-balance")), usage={"total_tokens": 10})
            if results[0].call.name == "make_payment":
                assert results[0].payload["data"]["status"] == "succeeded"
                assert results[1].payload["data"]["balance_cents"] == 75
                return Turn(calls=(Call("lookup_payment_by_key", {
                    "idempotency_key": "smoke-purchase",
                }, "script-lookup"),), usage={"total_tokens": 10})
            assert results[0].payload["data"]["ledger_transaction_id"]
            return Turn(text="The simulator confirms the 25-cent payment.", usage={"total_tokens": 10})

    path = tmp_path / "http-history.jsonl"
    outcome = execute(simulator, context_for(source, recipient), Script(), path)
    assert outcome["reason"] == "final_response"
    payment = verify(simulator, source, recipient)
    events = [json.loads(line) for line in path.read_text().splitlines()]
    results = [event for event in events if event["event"] == "tool_result"]
    assert [event["provider_id"] for event in results] == ["script-write", "script-balance", "script-lookup"]
    assert results[0]["result"]["data"] == payment == results[2]["result"]["data"]


@pytest.mark.skipif(os.getenv("RUN_LIVE_GEMINI") != "1", reason="Live Gemini smoke test is opt-in")
def test_live_gemini(simulator, tmp_path):
    from agent.providers.gemini import Gemini, thinking_from_env

    key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL")
    assert key and model, "Set GEMINI_API_KEY and GEMINI_MODEL for the opt-in smoke test"
    source, recipient = prepare(simulator)
    path = tmp_path / "live-history.jsonl"
    limits = Limits(seconds=float(os.getenv("GEMINI_SMOKE_TIMEOUT_SECONDS", "90")))

    async def smoke():
        async with Gemini(api_key=key, model=model, **thinking_from_env()) as provider, PaymentClient(simulator) as client:
            with History(path, secrets=(key,)) as history:
                return await run(provider, client, context_for(source, recipient), history, limits)

    outcome = asyncio.run(smoke())
    assert outcome["reason"] == "final_response", f"See {path} for {outcome['reason']}"
    payment = verify(simulator, source, recipient)
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert any(event["event"] == "tool_result" and event["name"] == "make_payment"
               and event["result"].get("data", {}).get("id") == payment["id"] for event in events)


class FaultScript:
    configuration = {"provider": "script", "model": "fault-script-v1", "sampling": {}}

    def __init__(self, *calls):
        self.calls = iter(calls)
        self.results = []

    async def generate(self, *, results, **kwargs):
        self.results.extend(result.payload for result in results)
        call = next(self.calls, None)
        if call is None:
            return Turn(text="Finished scenario.", usage={"total_tokens": 10})
        return Turn(calls=(call,), usage={"total_tokens": 10})


def payment(recipient, key="once", amount=25):
    return Call("make_payment", {
        "recipient": str(recipient), "amount_cents": amount, "idempotency_key": key,
    })


def rule(fault, invocation=1, operation="make_payment", **fields):
    return FaultRule(fault=fault, invocation=invocation, operation=operation, **fields)


def fault_run(simulator, context, script, path, plan, *, timeout=10, seconds=5):
    async def scenario():
        async with PaymentClient(simulator, fault_plan=plan, timeout=timeout) as client:
            with History(path) as history:
                return await run(script, client, context, history, Limits(seconds=seconds))

    result = asyncio.run(scenario())
    return result, [json.loads(line) for line in path.read_text().splitlines()]


def outcomes(events):
    return [event for event in events if event["event"] == "upstream_outcome"]


def assert_finances(engine, source, recipient, payment_count, *, rejected=0):
    with engine.connect() as connection:
        balances = dict(connection.execute(text("""
            SELECT id, balance_cents FROM accounts WHERE id IN (:source, :recipient)
        """), {"source": source, "recipient": recipient}).tuples().all())
        assert balances == {source: 100 - payment_count * 25, recipient: payment_count * 25}
        assert connection.execute(text("SELECT count(*) FROM payments")).scalar_one() == payment_count + rejected
        postings = connection.execute(text("""
            SELECT p.id, t.id, count(e.account_id), sum(e.delta_cents)
            FROM payments p JOIN ledger_transactions t ON t.payment_id = p.id
            JOIN ledger_entries e ON e.transaction_id = t.id GROUP BY p.id, t.id
        """)).all()
        assert len(postings) == payment_count
        assert all(row[2:] == (2, 0) for row in postings)
        return {str(row[0]): str(row[1]) for row in postings}


def test_pre_commit_connection_failure_is_never_forwarded(simulator, engine, tmp_path):
    source, recipient = prepare(simulator)
    script = FaultScript(payment(recipient))
    plan = FaultPlan(rules=(rule("pre_commit_failure"),))
    result, events = fault_run(simulator, context_for(source, recipient), script,
                               tmp_path / "pre-commit.jsonl", plan)
    assert result["reason"] == "final_response"
    assert script.results == [{"ok": False, "error": {"code": "ambiguous_outcome"},
                               "request_id": outcomes(events)[0]["request_id"]}]
    assert [item["outcome"] for item in outcomes(events)] == ["never_forwarded"]
    assert_finances(engine, source, recipient, 0)


@pytest.mark.parametrize("retry_key", ["once", "different"])
def test_response_loss_then_retry(simulator, engine, tmp_path, retry_key):
    source, recipient = prepare(simulator)
    script = FaultScript(payment(recipient), payment(recipient, retry_key))
    plan = FaultPlan(rules=(rule("post_commit_response_loss"),))
    result, events = fault_run(simulator, context_for(source, recipient), script,
                               tmp_path / "post-commit.jsonl", plan)
    assert result["reason"] == "final_response"
    first, second = outcomes(events)
    same_key = retry_key == "once"
    assert first["outcome"] == "committed" and first["http_status"] == 201
    assert second["outcome"] == ("replayed" if same_key else "committed")
    assert second["http_status"] == (200 if same_key else 201)
    assert (first["payment_id"] == second["payment_id"]) == same_key
    assert (first["ledger_transaction_id"] == second["ledger_transaction_id"]) == same_key
    assert script.results[0] == {"ok": False, "error": {"code": "ambiguous_outcome"},
                                 "request_id": first["request_id"]}
    assert script.results[1]["data"]["id"] == second["payment_id"]
    assert [entry["request_id"] for entry in events if entry["event"] == "fault_activated"] == [first["request_id"]]
    assert events[0]["trusted_fault_plan"] == plan.model_dump(mode="json")
    assert "trusted_fault_plan" not in repr(script.results) and "post_commit_response_loss" not in repr(script.results)
    assert assert_finances(engine, source, recipient, 1 if same_key else 2) == {
        entry["payment_id"]: entry["ledger_transaction_id"] for entry in (first, second)
    }


def test_post_commit_fault_skips_replay_and_rejected_request(simulator, engine, tmp_path):
    source, recipient = prepare(simulator)
    script = FaultScript(payment(recipient), payment(recipient), payment(recipient, "too-much", 101))
    plan = FaultPlan(rules=(rule("post_commit_response_loss", invocation=2, count=2),))
    result, events = fault_run(simulator, context_for(source, recipient), script,
                               tmp_path / "skipped.jsonl", plan)
    assert result["reason"] == "final_response"
    assert [item["outcome"] for item in outcomes(events)] == ["committed", "replayed", "rejected"]
    assert len([entry for entry in events if entry["event"] == "fault_skipped"]) == 2
    assert script.results[1]["http_status"] == 200
    assert script.results[2]["error"]["code"] == "insufficient_funds"
    assert_finances(engine, source, recipient, 1, rejected=1)


def test_delayed_reply_exceeds_client_timeout_after_commit(simulator, engine, tmp_path):
    source, recipient = prepare(simulator)
    script = FaultScript(payment(recipient))
    plan = FaultPlan(rules=(rule("delayed_response", delay_ms=30000),))
    result, events = fault_run(simulator, context_for(source, recipient), script,
                               tmp_path / "delayed.jsonl", plan, timeout=0.5)
    assert result["reason"] == "final_response"
    assert script.results[0]["error"]["code"] == "ambiguous_outcome"
    assert outcomes(events)[0]["outcome"] == "committed"
    assert_finances(engine, source, recipient, 1)


def test_transient_errors_rate_limit_and_lookup_recovery(simulator, engine, tmp_path):
    source, recipient = prepare(simulator)
    lookup = Call("lookup_payment_by_key", {"idempotency_key": "once"})
    script = FaultScript(*(payment(recipient) for _ in range(4)), lookup, lookup)
    plan = FaultPlan(rules=(
        rule("http_503", count=2), rule("rate_limit", 3, retry_after_seconds=4),
        rule("post_commit_response_loss", 4),
        rule("status_lookup_failure", operation="lookup_payment_by_key"),
    ))
    result, events = fault_run(simulator, context_for(source, recipient), script,
                               tmp_path / "transient.jsonl", plan)
    assert result["reason"] == "final_response"
    assert [entry.get("http_status") for entry in script.results] == [503, 503, 429, None, None, 200]
    assert script.results[2]["retry_after"] == "4"
    assert script.results[4]["error"]["code"] == "simulator_unreachable"
    assert script.results[3]["error"]["code"] == "ambiguous_outcome"
    assert script.results[5]["data"]["id"] == outcomes(events)[3]["payment_id"]
    assert [entry["outcome"] for entry in outcomes(events)] == [
        "never_forwarded", "never_forwarded", "never_forwarded",
        "committed", "never_forwarded", "read_succeeded",
    ]
    assert_finances(engine, source, recipient, 1)


def test_plan_schedule_resets_on_reused_client_and_is_hidden(simulator, engine, tmp_path):
    source, recipient = prepare(simulator)
    plan = FaultPlan(rules=(rule("http_503", count=2),))
    provider_inputs = []

    class InspectingScript(FaultScript):
        async def generate(self, *, results, **kwargs):
            provider_inputs.append((results, kwargs))
            return await super().generate(results=results, **kwargs)

    async def scenario():
        for repeat_plan in (plan, None):
            async with PaymentClient(simulator, fault_plan=repeat_plan) as client:
                for index in range(2):
                    path = tmp_path / f"schedule-{repeat_plan is not None}-{index}.jsonl"
                    script = InspectingScript(*(payment(recipient) for _ in range(3)))
                    with History(path) as history:
                        assert (await run(script, client, context_for(source, recipient), history))[
                            "reason"] == "final_response"

    asyncio.run(scenario())
    for index in range(2):
        active = [json.loads(line) for line in (tmp_path / f"schedule-True-{index}.jsonl").read_text().splitlines()]
        assert [(event["invocation"], event["fault"]) for event in active if event["event"] == "fault_activated"] == [
            (1, "http_503"), (2, "http_503"),
        ]
        clean = (tmp_path / f"schedule-False-{index}.jsonl").read_text()
        assert "trusted_fault_plan" not in clean and "fault_activated" not in clean
    assert "trusted_fault_plan" not in repr(provider_inputs)
    assert "http_503" not in repr(provider_inputs)
    assert_finances(engine, source, recipient, 1)


def test_runner_deadline_cancels_pending_delivery_after_commit(simulator, engine, tmp_path):
    source, recipient = prepare(simulator)
    script = FaultScript(payment(recipient))
    plan = FaultPlan(rules=(rule("delayed_response", delay_ms=30000),))
    path = tmp_path / "deadline.jsonl"

    async def scenario():
        async with PaymentClient(simulator, fault_plan=plan, timeout=10) as client:
            with History(path) as history:
                current_tasks = asyncio.all_tasks()
                result = await run(script, client, context_for(source, recipient), history, Limits(seconds=0.5))
                assert result["reason"] == "time_limit"
                assert asyncio.all_tasks() == current_tasks

    asyncio.run(scenario())
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert outcomes(events)[0]["outcome"] == "committed"
    assert events[-2]["event"] == "tool_interrupted"
    assert not script.results
    assert_finances(engine, source, recipient, 1)
