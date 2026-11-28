from contextlib import asynccontextmanager, nullcontext
from uuid import UUID

import httpx

from agent.faults import FaultPlan, FaultTransport


def committed_payment(data: dict, body: dict) -> bool:
    if data.get("status") != "succeeded" or any(data.get(key) != value for key, value in body.items()):
        return False
    try:
        UUID(data.get("id", ""))
        UUID(data.get("ledger_transaction_id", ""))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


class PaymentClient:
    """A single configured simulator origin, with one HTTP attempt per invocation."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000",
                 *, transport: httpx.AsyncBaseTransport | None = None,
                 fault_plan: FaultPlan | None = None, timeout: float = 10):
        url = httpx.URL(base_url)
        if (url.scheme not in {"http", "https"} or not url.host or url.userinfo
                or url.query or url.fragment or url.path not in {"", "/"}):
            raise ValueError("Simulator URL must be an HTTP(S) origin without credentials")
        upstream = transport if transport is not None else httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.fault_plan = fault_plan
        self.fault_transport = (FaultTransport(upstream, fault_plan, committed_payment)
                                if fault_plan is not None else None)
        self.http = httpx.AsyncClient(
            base_url=url, follow_redirects=False, trust_env=False, timeout=timeout,
            transport=self.fault_transport if self.fault_transport is not None else upstream,
        )

    @asynccontextmanager
    async def fault_scope(self, record):
        with self.fault_transport.execution(record) if self.fault_transport is not None else nullcontext():
            yield

    async def __aenter__(self):
        await self.http.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.http.__aexit__(*exc)

    async def request(self, method: str, path: str, correlation_id: str, *,
                      source: UUID, key: str | None = None, body: dict | None = None,
                      params: dict | None = None, check_owner: bool = False) -> dict:
        headers = {"X-Request-ID": correlation_id}
        if key is not None:
            headers["Idempotency-Key"] = key
        result = {"request_id": correlation_id}
        try:
            response = await self.http.request(method, path, headers=headers, json=body, params=params)
        except httpx.RequestError:
            return result | {"ok": False, "error": {
                "code": "ambiguous_outcome" if method == "POST" else "simulator_unreachable"
            }}
        result |= {"http_status": response.status_code,
                   "request_id": response.headers.get("X-Request-ID", correlation_id)}
        if response.status_code == 429 and "Retry-After" in response.headers:
            result["retry_after"] = response.headers["Retry-After"]
        try:
            data = response.json()
        except ValueError:
            data = None
        if (response.is_success and isinstance(data, dict)
                and (method != "POST" or committed_payment(data, body or {}))):
            if check_owner and data.get("source_account_id") != str(source):
                return result | {"ok": False, "error": {"code": "payment_not_found"}}
            return result | {"ok": True, "data": data}
        if (400 <= response.status_code < 500 and isinstance(data, dict)
                and isinstance(data.get("error"), dict)):
            return result | {"ok": False, "error": data["error"]}
        return result | {"ok": False, "error": {
            "code": "ambiguous_outcome" if method == "POST" else "unexpected_simulator_response"
        }}
