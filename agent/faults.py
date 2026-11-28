"""Opt-in, execution-local faults at the simulator HTTP transport boundary."""
import asyncio
import json
from collections.abc import Callable
from contextlib import contextmanager
from typing import Annotated, Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

Operation = Literal[
    "make_payment", "check_payment_status", "lookup_payment_by_key",
    "get_balance", "get_transaction_history",
]
Fault = Literal[
    "pre_commit_failure", "post_commit_response_loss", "delayed_response",
    "http_503", "rate_limit", "status_lookup_failure",
]
Positive = Annotated[int, Field(strict=True, gt=0)]


class FaultRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Operation
    invocation: Positive
    fault: Fault
    count: Positive = 1
    delay_ms: Positive | None = None
    retry_after_seconds: Positive | None = None

    @model_validator(mode="after")
    def valid_target_and_parameters(self) -> Self:
        if self.fault in {"pre_commit_failure", "post_commit_response_loss"}:
            if self.operation != "make_payment":
                raise ValueError("Commit faults require make_payment")
        if self.fault == "status_lookup_failure":
            if self.operation not in {"check_payment_status", "lookup_payment_by_key"}:
                raise ValueError("Status lookup faults require a payment lookup operation")
        if (self.delay_ms is not None) != (self.fault == "delayed_response"):
            raise ValueError("delay_ms is required only for delayed_response")
        if (self.retry_after_seconds is not None) != (self.fault == "rate_limit"):
            raise ValueError("retry_after_seconds is required only for rate_limit")
        return self


class FaultPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rules: tuple[FaultRule, ...]

    @model_validator(mode="after")
    def disjoint_rules(self) -> Self:
        previous_end = {}
        for rule in sorted(self.rules, key=lambda item: (item.operation, item.invocation)):
            if rule.invocation < previous_end.get(rule.operation, 1):
                raise ValueError(f"Overlapping rules for {rule.operation}")
            previous_end[rule.operation] = rule.invocation + rule.count
        return self


def operation_for(request: httpx.Request) -> Operation | None:
    path = request.url.path
    if request.method == "POST" and path == "/payments":
        return "make_payment"
    if request.method != "GET":
        return None
    if path == "/payments/by-key":
        return "lookup_payment_by_key"
    if path.startswith("/payments/"):
        return "check_payment_status"
    if path.startswith("/accounts/") and path.endswith("/balance"):
        return "get_balance"
    if path.startswith("/accounts/") and path.endswith("/transactions"):
        return "get_transaction_history"
    return None


class FaultTransport(httpx.AsyncBaseTransport):
    def __init__(self, upstream: httpx.AsyncBaseTransport, plan: FaultPlan,
                 valid_receipt: Callable[[dict, dict], bool]):
        self.upstream = upstream
        self.plan = plan
        self.valid_receipt = valid_receipt
        self.record = None
        self.invocations: dict[Operation, int] = {}

    @contextmanager
    def execution(self, record):
        if self.record is not None:
            raise RuntimeError("A fault-enabled client already has an active execution")
        self.record = record
        try:
            yield
        finally:
            self.record = None
            self.invocations.clear()

    async def __aenter__(self):
        await self.upstream.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.upstream.__aexit__(*exc)

    async def aclose(self):
        await self.upstream.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        operation = operation_for(request)
        if self.record is None or operation is None:
            return await self.upstream.handle_async_request(request)

        invocation = self.invocations.get(operation, 0) + 1
        self.invocations[operation] = invocation
        rule = next((rule for rule in self.plan.rules if rule.operation == operation
                     and rule.invocation <= invocation < rule.invocation + rule.count), None)
        record = self.record
        fields = {"operation": operation, "invocation": invocation,
                  "request_id": request.headers.get("X-Request-ID"),
                  "idempotency_key": request.headers.get("Idempotency-Key")}

        def activate():
            record("fault_activated", **fields, fault=rule.fault)

        pre_forward = {"pre_commit_failure", "http_503", "rate_limit", "status_lookup_failure"}
        if rule is not None and rule.fault in pre_forward:
            activate()
            record("upstream_outcome", **fields, outcome="never_forwarded")
            if rule.fault in {"pre_commit_failure", "status_lookup_failure"}:
                raise httpx.ConnectError("Simulator connection failed", request=request)
            headers = {"X-Request-ID": fields["request_id"]} if fields["request_id"] else {}
            if rule.fault == "rate_limit":
                headers["Retry-After"] = str(rule.retry_after_seconds)
                return httpx.Response(429, headers=headers, json={"error": {"code": "rate_limited"}})
            return httpx.Response(503, headers=headers, json={"error": {"code": "service_unavailable"}})

        try:
            response = await self.upstream.handle_async_request(request)
            try:
                await response.aread()
            finally:
                await response.aclose()
        except (httpx.RequestError, asyncio.CancelledError):
            record("upstream_outcome", **fields, outcome="unknown")
            raise

        outcome = self.describe_response(request, response, operation)
        record("upstream_outcome", **fields, **outcome)
        if rule is not None and rule.fault == "post_commit_response_loss":
            if outcome["outcome"] == "committed":
                activate()
                raise httpx.ReadError("Simulator response was lost", request=request)
            record("fault_skipped", **fields, fault=rule.fault, reason="no_new_commit")
        if rule is not None and rule.fault == "delayed_response":
            activate()
            # HTTPX supplies read timeout metadata to transports. It cannot
            # enforce the timeout around our synthetic response wait itself.
            read_timeout = request.extensions.get("timeout", {}).get("read")
            try:
                async with asyncio.timeout(read_timeout):
                    await asyncio.sleep(rule.delay_ms / 1000)
            except TimeoutError as exc:
                raise httpx.ReadTimeout("Simulator response timed out", request=request) from exc
        return response

    def describe_response(self, request: httpx.Request, response: httpx.Response,
                          operation: Operation) -> dict:
        result = {"http_status": response.status_code, "outcome": "unknown"}
        try:
            data = response.json()
        except ValueError:
            return result
        if not isinstance(data, dict):
            return result
        if isinstance(data.get("error"), dict) and 400 <= response.status_code < 500:
            error = data["error"]
            return result | {"outcome": "rejected", "error_code": error.get("code"),
                             "payment_id": error.get("payment_id")}
        if operation == "make_payment":
            if response.status_code in {200, 201} and self.valid_receipt(data, json.loads(request.content)):
                return result | {
                    "outcome": "committed" if response.status_code == 201 else "replayed",
                    "payment_id": data["id"], "ledger_transaction_id": data["ledger_transaction_id"],
                }
        elif response.is_success:
            result["outcome"] = "read_succeeded"
            if operation in {"check_payment_status", "lookup_payment_by_key"}:
                result |= {"payment_id": data.get("id"), "ledger_transaction_id": data.get("ledger_transaction_id"),
                           "payment_status": data.get("status")}
        return result
