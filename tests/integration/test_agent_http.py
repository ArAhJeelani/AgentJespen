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

from agent.config import Limits
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
