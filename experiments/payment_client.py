import asyncio
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, TypeVar
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from app.schemas import AccountBalance, AccountRequest, History, MAX_CENTS, Payment, PaymentRequest

_KEY = re.compile(r"[!-~]{1,128}")
_UUID = TypeAdapter(UUID)
_REJECTIONS = {
    "source_not_found": 404,
    "recipient_not_found": 404,
    "insufficient_funds": 409,
    "balance_limit": 409,
}
_Model = TypeVar("_Model", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class RequestContext:
    agent_id: str | None = None
    experiment_id: str | None = None
    purchase_id: str | None = None


@dataclass(frozen=True, slots=True)
class PaymentAttempt:
    purchase_id: str | None
    idempotency_key: str
    request_id: str
    agent_id: str | None = None
    experiment_id: str | None = None


class InvalidClientRequest(ValueError):
    """Input failed local validation before any request was sent."""


class _ErrorBody(BaseModel):
    model_config = ConfigDict(strict=True)

    code: str
    message: str
    request_id: str
    payment_id: UUID | None = None
    details: list[dict[str, Any]] | None = None


class _ErrorEnvelope(BaseModel):
    error: _ErrorBody


class PaymentClientError(Exception):
    """An unsuccessful client observation with a machine-readable category."""

    def __init__(
        self, category: str, request_id: str, status_code: int | None,
        backend_error: _ErrorBody | None = None,
    ) -> None:
        self.category = category
        self.request_id = request_id
        self.status_code = status_code
        self.backend_code = backend_error.code if backend_error else None
        self.backend_request_id = backend_error.request_id if backend_error else None
        self.payment_id = backend_error.payment_id if backend_error else None
        self.details = backend_error.details if backend_error else None
        description = backend_error.message if backend_error else category
        super().__init__(f"{description} (request {request_id})")


class PaymentRejected(PaymentClientError):
    """The service returned a persisted payment rejection."""


class UnknownWriteOutcome(PaymentClientError):
    """The communication result cannot establish whether a write committed."""


class UnknownPaymentOutcome(UnknownWriteOutcome):
    """The submitted payment may have committed."""


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise InvalidClientRequest("Idempotency-Key must have 1–128 printable non-space ASCII characters")
    return key


def _validate_uuid(value: UUID) -> UUID:
    try:
        return _UUID.validate_python(value)
    except ValidationError as exc:
        raise InvalidClientRequest("Expected a UUID") from exc


class PaymentClient:
    """An async client for AgentJepsen's simulated payment service.

    Supply a base URL to let this object own its HTTP client, or pass a
    configured AsyncClient whose lifetime and transport policy you manage.
    Share an instance among tasks running on the same event loop.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        record_attempt: Callable[[PaymentAttempt], None] | None = None,
        *,
        base_url: str | httpx.URL | None = None,
        timeout: float | httpx.Timeout = 5.0,
        limits: httpx.Limits = httpx.Limits(max_connections=100, max_keepalive_connections=20),
        transport: httpx.AsyncBaseTransport | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if http_client is None:
            url = httpx.URL(base_url or "")
            if url.scheme not in ("http", "https") or not url.host:
                raise ValueError("An absolute HTTP(S) payment service base URL is required")
            self._http_client = httpx.AsyncClient(
                base_url=url, timeout=timeout, limits=limits, transport=transport, trust_env=False,
            )
        else:
            if base_url is not None or transport is not None:
                raise ValueError("Configure the base URL and transport on the supplied HTTP client")
            self._http_client = http_client
        self._owns_client = http_client is None
        self._record_attempt = record_attempt
        self._logger = logger if logger is not None else logging.getLogger(__name__)

    async def __aenter__(self) -> "PaymentClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http_client.aclose()

    async def create_account(
        self, opening_balance_cents: int = 0, *, context: RequestContext = RequestContext(),
    ) -> AccountBalance:
        try:
            payload = AccountRequest(opening_balance_cents=opening_balance_cents)
        except ValidationError as exc:
            raise InvalidClientRequest("Invalid opening balance in cents") from exc
        return await self._request(
            "create_account", "POST", "/accounts", AccountBalance, (201,), context,
            payload=payload, unknown=UnknownWriteOutcome,
        )

    async def check_balance(
        self, account_id: UUID, *, context: RequestContext = RequestContext(),
    ) -> AccountBalance:
        return await self._request(
            "check_balance", "GET", f"/accounts/{_validate_uuid(account_id)}/balance",
            AccountBalance, (200,), context,
        )

    async def pay(
        self, payload: PaymentRequest, *, idempotency_key: str,
        purchase_id: str | None = None, context: RequestContext = RequestContext(),
    ) -> Payment:
        """Submit one payment. A caller may explicitly look up or resubmit it."""
        if not isinstance(payload, PaymentRequest):
            raise InvalidClientRequest("Expected a PaymentRequest")
        try:
            payload = PaymentRequest(
                source_account_id=payload.source_account_id,
                destination_account_id=payload.destination_account_id,
                amount_cents=payload.amount_cents,
            )
        except ValidationError as exc:
            raise InvalidClientRequest("Invalid payment payload") from exc
        if purchase_id is not None:
            if context.purchase_id is not None and purchase_id != context.purchase_id:
                raise InvalidClientRequest("Conflicting logical purchase IDs")
            context = RequestContext(context.agent_id, context.experiment_id, purchase_id)
        return await self._request(
            "pay", "POST", "/payments", Payment, (200, 201), context,
            payload=payload, key=_validate_key(idempotency_key), unknown=UnknownPaymentOutcome,
        )

    async def check_payment_status(
        self, payment_id: UUID, *, context: RequestContext = RequestContext(),
    ) -> Payment:
        return await self._request(
            "check_payment_status", "GET", f"/payments/{_validate_uuid(payment_id)}",
            Payment, (200,), context,
        )

    async def check_payment_by_key(
        self, idempotency_key: str, *, context: RequestContext = RequestContext(),
    ) -> Payment:
        return await self._request(
            "check_payment_by_key", "GET", "/payments/by-key", Payment, (200,), context,
            key=_validate_key(idempotency_key),
        )

    async def get_transaction_history(
        self, account_id: UUID, *, offset: int = 0, limit: int = 50,
        context: RequestContext = RequestContext(),
    ) -> History:
        if type(offset) is not int or not 0 <= offset <= MAX_CENTS:
            raise InvalidClientRequest("Offset must be an integer between 0 and 2**63 - 1")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidClientRequest("Limit must be an integer between 1 and 100")
        return await self._request(
            "get_transaction_history", "GET", f"/accounts/{_validate_uuid(account_id)}/transactions",
            History, (200,), context, params={"offset": offset, "limit": limit},
        )

    async def _request(
        self, operation: str, method: str, path: str, model: type[_Model],
        expected: tuple[int, ...], context: RequestContext, *,
        payload: BaseModel | None = None, key: str | None = None,
        params: dict[str, int] | None = None,
        unknown: type[UnknownWriteOutcome] | None = None,
    ) -> _Model:
        request_id = uuid4().hex
        headers = {"X-Request-ID": request_id}
        if key is not None:
            headers["Idempotency-Key"] = key
        started_at = datetime.now(timezone.utc)
        started = perf_counter()
        status = None
        backend_request_id = None
        payment_id = None
        transaction_id = None
        exception_category = None
        exception_type = None
        outcome = "client_error"
        uncertain = unknown or PaymentClientError
        try:
            if self._record_attempt is not None and operation == "pay" and key is not None:
                self._record_attempt(PaymentAttempt(
                    context.purchase_id, key, request_id, context.agent_id, context.experiment_id,
                ))
            try:
                response = await self._http_client.request(
                    method, path, headers=headers, params=params, follow_redirects=False,
                    **({"json": payload.model_dump(mode="json")} if payload is not None else {}),
                )
            except httpx.TimeoutException as exc:
                raise uncertain("timeout", request_id, None) from exc
            except httpx.NetworkError as exc:
                raise uncertain("connection_error", request_id, None) from exc
            except httpx.RequestError as exc:
                raise uncertain("transport_error", request_id, None) from exc

            status = response.status_code
            backend_request_id = response.headers.get("X-Request-ID")
            if status not in expected:
                self._raise_response_error(response, request_id, operation, uncertain)
            try:
                result = model.model_validate_json(response.content, strict=True)
            except ValidationError as exc:
                raise uncertain("malformed_response", request_id, status) from exc
            if isinstance(result, Payment):
                succeeded = result.status == "succeeded"
                if (
                    succeeded != (result.ledger_transaction_id is not None)
                    or succeeded != (result.failure_code is None)
                    or isinstance(payload, PaymentRequest) and (
                        not succeeded or any(
                            getattr(result, field) != value
                            for field, value in payload
                        )
                    )
                ):
                    raise uncertain("malformed_response", request_id, status)
                payment_id = result.id
                transaction_id = result.ledger_transaction_id
                outcome = result.status
            else:
                outcome = "response_received"
            return result
        except PaymentClientError as exc:
            payment_id = exc.payment_id
            exception_category = exc.category
            exception_type = type(exc).__name__
            if isinstance(exc, UnknownWriteOutcome):
                outcome = "unknown"
            elif isinstance(exc, PaymentRejected):
                outcome = "rejected"
            else:
                outcome = "request_error"
            raise
        except asyncio.CancelledError:
            exception_category = "cancelled"
            exception_type = "CancelledError"
            outcome = "unknown" if unknown else "cancelled"
            raise
        finally:
            self._logger.info("payment_service_request", extra={
                "operation": operation,
                "request_id": request_id,
                "backend_request_id": backend_request_id,
                "agent_id": context.agent_id,
                "experiment_id": context.experiment_id,
                "purchase_id": context.purchase_id,
                "idempotency_key_sha256": hashlib.sha256(key.encode("ascii")).hexdigest() if key else None,
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": (perf_counter() - started) * 1000,
                "status_code": status,
                "payment_id": str(payment_id) if payment_id else None,
                "transaction_id": str(transaction_id) if transaction_id else None,
                "observed_outcome": outcome,
                "exception_category": exception_category,
                "exception_type": exception_type,
            })

    @staticmethod
    def _raise_response_error(
        response: httpx.Response, request_id: str, operation: str,
        uncertain: type[PaymentClientError],
    ) -> None:
        status = response.status_code
        try:
            error = _ErrorEnvelope.model_validate_json(response.content).error
        except ValidationError:
            error = None
        if status == 503:
            raise uncertain("service_unavailable", request_id, status, error)
        if status >= 500:
            raise uncertain("server_error", request_id, status, error)
        if error is not None and error.request_id == request_id:
            if status == 422 and error.code == "invalid_request":
                raise PaymentClientError("backend_validation", request_id, status, error)
            if operation == "pay":
                if error.payment_id is not None and _REJECTIONS.get(error.code) == status:
                    raise PaymentRejected(error.code, request_id, status, error)
                if status == 409 and error.code == "idempotency_conflict":
                    raise PaymentClientError("idempotency_conflict", request_id, status, error)
            if operation == "create_account" and status == 409 and error.code == "issuance_limit":
                raise PaymentClientError("issuance_limit", request_id, status, error)
            if status == 404 and error.code in ("payment_not_found", "account_not_found"):
                if operation not in ("pay", "create_account"):
                    raise PaymentClientError(error.code, request_id, status, error)
        category = "malformed_response" if error is None and 400 <= status < 500 else "unexpected_response"
        raise uncertain(category, request_id, status, error)
