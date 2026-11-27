import hashlib
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from time import perf_counter
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine
from starlette.exceptions import HTTPException

from app import payments
from app.db import database_url, postgres_engine
from app.schemas import AccountBalance, AccountRequest, History, Payment, PaymentRequest

KEY = Annotated[str, Header(alias="Idempotency-Key", pattern=r"^[!-~]{1,128}$")]
REQUEST_ID = re.compile(r"[a-zA-Z0-9_.-]{1,64}")
logger = logging.getLogger("agentjepsen.events")
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())
logger.propagate = False


def event(name: str, request: Request, **fields) -> None:
    logger.info(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": name,
        "request_id": request.state.request_id,
        **fields,
    }, default=str))


def error_response(status: int, code: str, message: str, request: Request, **fields):
    return JSONResponse(
        status_code=status,
        content={"error": {
            "code": code, "message": message, "request_id": request.state.request_id, **fields
        }},
    )


def create_app(engine: Engine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(api: FastAPI):
        api.state.engine = engine if engine is not None else postgres_engine(database_url())
        try:
            yield
        finally:
            if engine is None:
                api.state.engine.dispose()

    api = FastAPI(title="AgentJepsen Payments", version="0.1.0", lifespan=lifespan)

    @api.middleware("http")
    async def correlate(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request.state.request_id = supplied if REQUEST_ID.fullmatch(supplied) else str(uuid4())
        started = perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            event("request_failed", request, exception_type=type(exc).__name__)
            response = error_response(500, "internal_error", "Request could not be completed", request)
        response.headers["X-Request-ID"] = request.state.request_id
        route = request.scope.get("route")
        event("request_completed", request, method=request.method,
              route=getattr(route, "path", None), status=response.status_code,
              duration_ms=round((perf_counter() - started) * 1000, 3))
        return response

    @api.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        details = [{"location": entry["loc"], "message": entry["msg"]} for entry in exc.errors()]
        return error_response(422, "invalid_request", "Request validation failed", request, details=details)

    @api.exception_handler(payments.PaymentError)
    async def payment_error(request: Request, exc: payments.PaymentError):
        status, message = payments.ERRORS[exc.code]
        return error_response(status, exc.code, message, request)

    @api.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        code = {404: "route_not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        response = error_response(exc.status_code, code, str(exc.detail), request)
        response.headers.update(exc.headers or {})
        return response

    @api.post("/accounts", status_code=201, response_model=AccountBalance)
    def create_account(payload: AccountRequest, request: Request, response: Response,
                       idempotency_key: Annotated[str | None, Header(
                           alias="Idempotency-Key", pattern=r"^[!-~]{1,128}$"
                       )] = None):
        account, journal_id, replay = payments.create_account(
            request.app.state.engine, payload.opening_balance_cents, request.state.request_id, idempotency_key
        )
        if replay:
            response.status_code = 200
        event("account_replayed" if replay else "account_created", request,
              account_id=account["id"], ledger_transaction_id=journal_id)
        return account

    @api.get("/accounts/{account_id}/balance", response_model=AccountBalance)
    def balance(account_id: UUID, request: Request):
        with request.app.state.engine.connect() as connection:
            return payments.get_account(connection, account_id)

    @api.post("/payments", status_code=201, response_model=Payment)
    def create_payment(payload: PaymentRequest, request: Request, response: Response, idempotency_key: KEY):
        key_digest = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
        event("payment_attempted", request, key_sha256=key_digest)
        try:
            payment, replay = payments.create_payment(
                request.app.state.engine, payload, idempotency_key, request.state.request_id
            )
        except payments.PaymentError as exc:
            event("payment_conflict", request, key_sha256=key_digest, error_code=exc.code)
            raise
        outcome = "payment_replayed" if replay else (
            "payment_committed" if payment["status"] == "succeeded" else "payment_rejected"
        )
        event(outcome, request, key_sha256=key_digest, payment_id=payment["id"],
              ledger_transaction_id=payment["ledger_transaction_id"], failure_code=payment["failure_code"])
        if code := payment["failure_code"]:
            status, message = payments.ERRORS[code]
            return error_response(status, code, message, request, payment_id=str(payment["id"]))
        if replay:
            response.status_code = 200
        return payment

    @api.get("/payments/by-key", response_model=Payment)
    def payment_by_key(request: Request, idempotency_key: KEY):
        with request.app.state.engine.connect() as connection:
            return payments.find_payment(connection, key=idempotency_key)

    @api.get("/payments/{payment_id}", response_model=Payment)
    def payment_status(payment_id: UUID, request: Request):
        with request.app.state.engine.connect() as connection:
            return payments.find_payment(connection, payment_id=payment_id)

    @api.get("/accounts/{account_id}/transactions", response_model=History)
    def history(account_id: UUID, request: Request,
                offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
                limit: Annotated[int, Query(ge=1, le=100)] = 50):
        with request.app.state.engine.connect() as connection:
            return payments.account_history(connection, account_id, offset, limit)

    return api


app = create_app()
