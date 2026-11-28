import asyncio
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from agent.faults import FaultPlan, FaultRule
from agent.payment_client import PaymentClient


@pytest.mark.parametrize("rules", [
    [{"operation": "make_payment", "invocation": 0, "fault": "http_503"}],
    [{"operation": "make_payment", "invocation": 1, "fault": "status_lookup_failure"}],
    [{"operation": "get_balance", "invocation": 1, "fault": "post_commit_response_loss"}],
    [{"operation": "make_payment", "invocation": 1, "fault": "rate_limit"}],
    [{"operation": "make_payment", "invocation": 1, "fault": "delayed_response"}],
    [{"operation": "make_payment", "invocation": 1, "fault": "http_503", "delay_ms": 3}],
    [{"operation": "make_payment", "invocation": 1, "fault": "http_503", "count": 2},
     {"operation": "make_payment", "invocation": 2, "fault": "pre_commit_failure"}],
])
def test_invalid_fault_schedule(rules):
    with pytest.raises(ValidationError):
        FaultPlan.model_validate({"rules": rules})


def test_custom_transport_uses_httpx_read_timeout():
    async def check():
        source = uuid4()
        sent = []
        events = []

        def upstream(request):
            sent.append(request)
            return httpx.Response(200, json={"balance_cents": 50})

        def record(kind, **fields):
            events.append((kind, fields))

        plan = FaultPlan(rules=(FaultRule(operation="get_balance", invocation=1,
                                          fault="delayed_response", delay_ms=5000),))
        async with PaymentClient(transport=httpx.MockTransport(upstream), fault_plan=plan,
                                 timeout=0.02) as client, client.fault_scope(record):
            with pytest.raises(httpx.ReadTimeout, match="Simulator response timed out"):
                await asyncio.wait_for(client.http.get(f"/accounts/{source}/balance"), timeout=2)
        assert [kind for kind, _ in events] == ["upstream_outcome", "fault_activated"]
        assert events[0][1]["outcome"] == "read_succeeded"
        assert len(sent) == 1 and sent[0].extensions["timeout"]["read"] == 0.02

    asyncio.run(check())


def test_cancelled_upstream_is_unknown_and_does_not_leave_a_task():
    async def check():
        entered = asyncio.Event()
        exited = asyncio.Event()
        events = []
        source = uuid4()

        async def upstream(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()

        async with PaymentClient(transport=httpx.MockTransport(upstream),
                                 fault_plan=FaultPlan(rules=())) as client:
            async with client.fault_scope(lambda kind, **fields: events.append((kind, fields))):
                tasks_before = asyncio.all_tasks()
                task = asyncio.create_task(client.request("POST", "/payments", "id", source=source))
                await asyncio.wait_for(entered.wait(), timeout=2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert exited.is_set()
                assert asyncio.all_tasks() == tasks_before
            assert [fields["outcome"] for kind, fields in events if kind == "upstream_outcome"] == ["unknown"]

    asyncio.run(check())
