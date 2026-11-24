import asyncio
import hashlib
import json
import logging
from uuid import uuid4

import httpx
import pytest

from app.schemas import MAX_CENTS, PaymentRequest
from experiments.payment_client import (
    InvalidClientRequest,
    PaymentClient,
    PaymentClientError,
    PaymentRejected,
    RequestContext,
    UnknownPaymentOutcome,
    UnknownWriteOutcome,
)

BASE_URL = "http://payments.test"
STAMP = "2026-01-01T00:00:00Z"


@pytest.fixture
def payload():
    return PaymentRequest(source_account_id=uuid4(), destination_account_id=uuid4(), amount_cents=75)


def success(payload, **fields):
    return {
        **payload.model_dump(mode="json"),
        "id": str(uuid4()),
        "status": "succeeded",
        "failure_code": None,
        "created_at": STAMP,
        "ledger_transaction_id": str(uuid4()),
        **fields,
    }


def error(request, status, code, **fields):
    return httpx.Response(status, json={"error": {
        "request_id": request.headers["X-Request-ID"], "message": code, "code": code, **fields,
    }})


def test_all_routes_and_exact_integer_cents(payload):
    payment = success(payload)
    account_id = payload.source_account_id
    account = {"id": str(account_id), "balance_cents": MAX_CENTS}
    posting = {
        "transaction_id": payment["ledger_transaction_id"], "payment_id": payment["id"],
        "delta_cents": -75, "created_at": STAMP,
    }
    requests = []

    def handle(request):
        requests.append(request)
        responses = [
            (201, account), (200, account), (201, payment), (200, payment),
            (200, payment), (200, {"entries": [posting], "next_offset": 8}),
        ]
        status, body = responses[len(requests) - 1]
        return httpx.Response(status, json=body)

    async def run():
        async with PaymentClient(base_url=BASE_URL, transport=httpx.MockTransport(handle)) as client:
            assert (await client.create_account(MAX_CENTS)).balance_cents == MAX_CENTS
            assert (await client.check_balance(account_id)).id == account_id
            paid = await client.pay(payload, idempotency_key="key")
            assert str(paid.id) == payment["id"]
            assert str(paid.ledger_transaction_id) == payment["ledger_transaction_id"]
            assert await client.check_payment_status(paid.id) == paid
            assert await client.check_payment_by_key("key") == paid
            history = await client.get_transaction_history(account_id, offset=7, limit=1)
            assert history.next_offset == 8
            assert history.entries[0].transaction_id == paid.ledger_transaction_id
            assert history.entries[0].delta_cents == -75

    asyncio.run(run())
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/accounts"), ("GET", f"/accounts/{account_id}/balance"),
        ("POST", "/payments"), ("GET", f"/payments/{payment['id']}"),
        ("GET", "/payments/by-key"), ("GET", f"/accounts/{account_id}/transactions"),
    ]
    assert json.loads(requests[0].content) == {"opening_balance_cents": MAX_CENTS}
    assert json.loads(requests[2].content) == payload.model_dump(mode="json")
    assert [r.headers.get("Idempotency-Key") for r in requests] == [None, None, "key", None, "key", None]
    assert dict(requests[-1].url.params) == {"offset": "7", "limit": "1"}
    assert len({r.headers["X-Request-ID"] for r in requests}) == 6


def test_explicit_resubmission_preserves_key_and_attempt_identity(payload, caplog):
    attempts = []
    deliveries = []
    body = success(payload)

    async def handle(request):
        attempt = attempts[-1]
        assert request.headers["Idempotency-Key"] == attempt.idempotency_key == "same-key"
        assert request.headers["X-Request-ID"] == attempt.request_id
        assert "experiment" not in request.content.decode()
        deliveries.append(request)
        if len(deliveries) == 1:
            raise httpx.ReadTimeout("Confirmation lost after simulated processing")
        return httpx.Response(200, json=body)

    async def run():
        async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handle)) as http:
            async with PaymentClient(http, attempts.append) as client:
                context = RequestContext("agent", "experiment", "purchase")
                with pytest.raises(UnknownPaymentOutcome, match="timeout"):
                    await client.pay(payload, idempotency_key="same-key", context=context)
                assert len(deliveries) == 1
                paid = await client.pay(payload, idempotency_key="same-key", context=context)
                assert str(paid.id) == body["id"]
            assert not http.is_closed

    with caplog.at_level(logging.INFO, logger="experiments.payment_client"):
        asyncio.run(run())
    assert len(deliveries) == 2
    assert [attempt.purchase_id for attempt in attempts] == ["purchase", "purchase"]
    assert attempts[0].request_id != attempts[1].request_id
    records = [r for r in caplog.records if r.msg == "payment_service_request"]
    assert [r.observed_outcome for r in records] == ["unknown", "succeeded"]
    assert records[1].transaction_id == body["ledger_transaction_id"]
    for record in records:
        assert (record.agent_id, record.experiment_id, record.purchase_id) == ("agent", "experiment", "purchase")
        assert record.duration_ms >= 0
        assert record.idempotency_key_sha256 == hashlib.sha256(b"same-key").hexdigest()
        assert "same-key" not in repr(record.__dict__)


@pytest.mark.parametrize(("failure", "category"), [
    (httpx.ConnectError("Undelivered"), "connection_error"),
    (httpx.ConnectTimeout("Undelivered"), "timeout"),
    (httpx.Response(500), "server_error"),
    (httpx.Response(503), "service_unavailable"),
    (httpx.Response(307, headers={"Location": "/payments"}), "unexpected_response"),
    (httpx.Response(201, content=b"bad JSON"), "malformed_response"),
    (httpx.Response(409, json={"error": {"code": "insufficient_funds"}}), "malformed_response"),
])
def test_ambiguous_payment_sends_one_request(payload, failure, category):
    requests = []

    def handle(request):
        requests.append(request)
        if isinstance(failure, Exception):
            raise failure
        return failure

    async def run():
        async with PaymentClient(base_url=BASE_URL, transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(UnknownPaymentOutcome) as caught:
                await client.pay(payload, idempotency_key="key")
            assert caught.value.category == category
            assert caught.value.request_id == requests[0].headers["X-Request-ID"]

    asyncio.run(run())
    assert len(requests) == 1


def test_backend_rejection_and_explicit_lookup(payload):
    payment_id = uuid4()
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "POST":
            return error(request, 409, "insufficient_funds", payment_id=str(payment_id))
        return httpx.Response(200, json=success(
            payload, id=str(payment_id), status="rejected",
            failure_code="insufficient_funds", ledger_transaction_id=None,
        ))

    async def run():
        async with PaymentClient(base_url=BASE_URL, transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(PaymentRejected) as caught:
                await client.pay(payload, idempotency_key="key")
            assert caught.value.category == "insufficient_funds"
            assert caught.value.payment_id == payment_id
            assert len(requests) == 1
            assert (await client.check_payment_status(payment_id)).status == "rejected"

    asyncio.run(run())
    assert len(requests) == 2


@pytest.mark.parametrize(("status", "code", "category"), [
    (422, "invalid_request", "backend_validation"),
    (409, "idempotency_conflict", "idempotency_conflict"),
])
def test_known_non_payment_errors(payload, status, code, category):
    requests = []

    def handle(request):
        requests.append(request)
        return error(request, status, code)

    async def run():
        async with PaymentClient(base_url=BASE_URL, transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(PaymentClientError) as caught:
                await client.pay(payload, idempotency_key="existing")
            assert not isinstance(caught.value, (PaymentRejected, UnknownWriteOutcome))
            assert caught.value.category == category

    asyncio.run(run())
    assert len(requests) == 1


def test_validation_and_strict_response(payload):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": str(uuid4()), "balance_cents": "999"})

    async def run():
        async with PaymentClient(base_url=BASE_URL, transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(InvalidClientRequest):
                await client.pay(payload, idempotency_key="bad key")
            with pytest.raises(InvalidClientRequest):
                await client.pay(payload.model_copy(update={"amount_cents": 1.5}), idempotency_key="key")
            with pytest.raises(InvalidClientRequest):
                await client.create_account(True)
            with pytest.raises(InvalidClientRequest):
                await client.get_transaction_history(uuid4(), limit=101)
            assert not requests
            with pytest.raises(PaymentClientError) as caught:
                await client.check_balance(payload.source_account_id)
            assert caught.value.category == "malformed_response"

    asyncio.run(run())
    assert len(requests) == 1


def test_account_creation_timeout_is_an_unknown_write():
    def handle(request):
        raise httpx.ReadTimeout("Account response lost")

    async def run():
        async with PaymentClient(base_url=BASE_URL, transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(UnknownWriteOutcome) as caught:
                await client.create_account()
            assert not isinstance(caught.value, UnknownPaymentOutcome)

    asyncio.run(run())


def test_same_purchase_with_different_keys_sends_both_attempts(payload):
    keys = []

    def handle(request):
        keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(201, json=success(payload))

    async def run():
        async with PaymentClient(base_url=BASE_URL, transport=httpx.MockTransport(handle)) as client:
            first = await client.pay(payload, purchase_id="purchase", idempotency_key="first")
            second = await client.pay(payload, purchase_id="purchase", idempotency_key="second")
            assert first.id != second.id

    asyncio.run(run())
    assert keys == ["first", "second"]


def test_concurrent_delayed_requests_and_owned_cleanup(payload, caplog):
    size = 64
    requests = {}

    async def run():
        all_arrived = asyncio.Event()

        async def handle(request):
            key = request.headers["Idempotency-Key"]
            requests[key] = request.headers["X-Request-ID"]
            if len(requests) == size:
                all_arrived.set()
            await all_arrived.wait()
            if key == "0":
                raise httpx.ConnectError("Undelivered")
            return httpx.Response(201, json=success(payload))

        class TrackedTransport(httpx.MockTransport):
            closed = False

            async def aclose(self):
                self.closed = True
                await super().aclose()

        transport = TrackedTransport(handle)
        async with PaymentClient(base_url=BASE_URL, transport=transport) as client:
            results = await asyncio.wait_for(asyncio.gather(*[
                client.pay(payload, idempotency_key=str(i), context=RequestContext(agent_id=str(i)))
                for i in range(size)
            ], return_exceptions=True), timeout=5)
            assert isinstance(results[0], UnknownPaymentOutcome)
            assert all(result.status == "succeeded" for result in results[1:])
        assert transport.closed

    with caplog.at_level(logging.INFO, logger="experiments.payment_client"):
        asyncio.run(run())
    records = [r for r in caplog.records if r.msg == "payment_service_request"]
    assert len({r.request_id for r in records}) == size
    assert {r.agent_id: r.request_id for r in records} == requests


def test_cancellation_preserves_asyncio_semantics_and_logs_unknown(payload, caplog):
    requests = []

    async def run():
        delivered = asyncio.Event()

        async def handle(request):
            requests.append(request)
            delivered.set()
            await asyncio.Future()

        async with PaymentClient(base_url=BASE_URL, transport=httpx.MockTransport(handle)) as client:
            task = asyncio.create_task(client.pay(payload, idempotency_key="cancel"))
            await asyncio.wait_for(delivered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    with caplog.at_level(logging.INFO, logger="experiments.payment_client"):
        asyncio.run(run())
    assert len(requests) == 1
    record, = [r for r in caplog.records if r.msg == "payment_service_request"]
    assert (record.observed_outcome, record.exception_category) == ("unknown", "cancelled")
