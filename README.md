# AgentJepsen

Phase 1 is a FastAPI service for simulated payments. PostgreSQL is the authoritative source for accounts, terminal payment outcomes, and ledger postings. The service has no LLM or external payment provider dependency.

## Run with Docker Compose

With Docker Engine or Docker Desktop running:

```sh
docker compose up --build -d
docker compose logs api
curl -fsS http://localhost:8000/openapi.json
```

The API runs Alembic before starting. Compose binds the API and PostgreSQL ports to loopback. To run tests against a dedicated database in the same PostgreSQL instance, create it once:

```sh
docker compose exec -T db createdb -U agentjepsen agentjepsen_test
docker compose run --rm api python -m pytest -q
```

## Run with native PostgreSQL

A reachable PostgreSQL server also works. Use Python 3.12 or newer and start PostgreSQL before running these commands. The migration role needs permission to create objects in the app schema. The test role needs permission to create schemas in the dedicated test database.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
createdb agentjepsen
createdb agentjepsen_test
export DATABASE_URL="postgresql+psycopg://localhost/agentjepsen"
export TEST_DATABASE_URL="postgresql+psycopg://localhost/agentjepsen_test"
alembic upgrade head
python -m pytest -q
uvicorn app.main:app
```

Adjust the URLs to match your PostgreSQL host, user, and credentials. To use a Unix socket, supply its directory as the URL's `host` query parameter. Integration tests fail explicitly if `TEST_DATABASE_URL` is missing or unreachable. Each integration test migrates its own uniquely named schema and drops it afterward. Run just `python -m pytest tests/test_validation.py -q` for the database-free validation tests.

## REST API

| Method and path | Result |
| --- | --- |
| `POST /accounts` | Creates a customer account. Send an object with optional `opening_balance_cents`, which defaults to zero. |
| `GET /accounts/{account_id}/balance` | Returns the current integer-cent balance. |
| `POST /payments` | Takes `source_account_id`, `destination_account_id`, and `amount_cents`. Requires an `Idempotency-Key` header of 1–128 printable non-space ASCII characters. |
| `GET /payments/{payment_id}` | Returns the terminal outcome, including the ledger transaction ID for successful payments. |
| `GET /payments/by-key` | Returns the same outcome using the `Idempotency-Key` header. |
| `GET /accounts/{account_id}/transactions` | Returns newest-first postings with `offset` and `limit`. Continue using `next_offset` until it is null. Maximum page size is 100. |

Create two accounts and use their returned IDs to send money:

```sh
curl -fsS http://localhost:8000/accounts \
  -H 'Content-Type: application/json' -d '{"opening_balance_cents":1000}'
curl -fsS http://localhost:8000/accounts \
  -H 'Content-Type: application/json' -d '{}'

source_id='REPLACE_WITH_FIRST_ACCOUNT_ID'
recipient_id='REPLACE_WITH_SECOND_ACCOUNT_ID'
curl -fsS http://localhost:8000/payments \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-purchase-1' \
  -H 'X-Request-ID: demo-attempt-1' \
  -d "{\"source_account_id\":\"$source_id\",\"destination_account_id\":\"$recipient_id\",\"amount_cents\":125}"
curl -fsS http://localhost:8000/payments/by-key -H 'Idempotency-Key: demo-purchase-1'
```

A successful new payment returns HTTP 201 and a successful replay returns HTTP 200. Well-formed requests that fail a business check return HTTP 404 for missing accounts or HTTP 409 for insufficient funds and balance limits. These responses include the stable payment ID, and lookup returns HTTP 200 with status `rejected`. Replaying a rejection preserves the original failure and payment ID. A changed payload under the same key returns HTTP 409 with code `idempotency_conflict`. Malformed input returns HTTP 422 before claiming a key.

Errors have an `error` object with `code`, `message`, and `request_id`, with additional fields for validation details or a persisted payment ID where applicable. Every response carries `X-Request-ID`. Clients can supply a 1–64 character ID made of letters, digits, dots, underscores, and hyphens. The service generates a UUID if the header is missing or invalid. Use a different request ID per attempt and reuse the payment key for retries.

Application events are JSON lines on stderr. Payment events carry a request ID, a SHA-256 digest of the key, and the payment and ledger IDs when available. A commit event is emitted after the database commits. The original request ID is persisted on payment and ledger rows, so a process crash between commit and logging leaves the authoritative transaction trace intact.

## Database and correctness

- **Money storage.** Amounts, postings, and balances are signed PostgreSQL `BIGINT` values. API validation accepts integer cents only. Customers have a database-enforced zero floor. The internal issuance account starts at zero and carries the opposite of all issued funds. Opening credits lock that account and check its signed range before posting.
- **Atomic writes.** Creating a funded account, debiting issuance, and recording both postings is one transaction. Transfers also write their outcome, balances, and journal in one transaction. The service uses `READ COMMITTED` and locks the two customer rows in ascending UUID order before checking available funds and recipient capacity.
- **Idempotency.** A unique index arbitrates key ownership before any account locks are acquired. Competing inserts wait for the owner to commit or roll back. After an insert loses, a separate statement sees the committed request and compares the source UUID, destination UUID, and cents. The same key gives at most one committed financial effect. Message delivery can be repeated or lost.
- **Rejected requests.** Payment rows store the requested UUIDs as input data without account foreign keys. This lets the service persist a rejection for an unknown UUID. Ledger entries reference real accounts through foreign keys. Deferred checks require a terminal payment state and ensure any successful payment matches the debit and credit in its one ledger transaction.
- **Immutable journal.** A deferred check requires each new ledger transaction to have two equal and opposite entries at commit. A unique constraint allows at most one debit and one credit, closing both slots in a finalized transaction. The primary key requires distinct account IDs. Statement triggers reject edits, deletion, and truncation of journal records.
- **Audit.** An account's current balance equals the sum of its postings through the service write paths. Including issuance, balances sum to zero. The PostgreSQL suite checks both properties after transfers, concurrent requests, and forced transaction failures.

## Phase 1 limits

The API assumes a trusted local simulator client. It has no authentication or purchase-level authorization. Idempotency keys are global and retained indefinitely. Different keys represent separate payment requests. Account creation has no idempotency key, so a blind retry after an ambiguous response can create a second account and issue a second opening balance.

The issuance row serializes funded account creation and imposes a total issuance cap of 2^63 cents. Offset-based history pagination can shift if postings arrive between page reads. The API stores balances for efficient reads and checks, so direct SQL changes outside the service can bypass balance-to-ledger reconciliation. The local migration role can also change schema protections. Treat direct database writers and the migration role as trusted.

Agent adapters, fault injection, behavioral invariant checks, and experiment execution are outside Phase 1.

## Async payment client

The client in [experiments/payment_client.py](experiments/payment_client.py) gives
scripted and future LLM agents the same typed interface to the payment service.
It reuses the API's Pydantic schemas and does not access the database.

| Client method | API request | Successful response | Documented domain errors |
| --- | --- | --- | --- |
| `create_account(opening_balance_cents=0)` | `POST /accounts`, opening balance from 0 through 2⁶³−1 | 201, account UUID and balance in cents | 409 `issuance_limit` |
| `check_balance(account_id)` | `GET /accounts/{account_id}/balance`, UUID | 200, account UUID and balance in cents | 404 `account_not_found` |
| `pay(payload, idempotency_key=...)` | `POST /payments`, two distinct account UUIDs, integer cents from 1 through 2⁶³−1, required key header | 201 for a new payment, 200 for a successful replay, with payment and ledger UUIDs | 404 `source_not_found` or `recipient_not_found`. 409 `insufficient_funds`, `balance_limit`, or `idempotency_conflict`. |
| `check_payment_status(payment_id)` | `GET /payments/{payment_id}`, UUID | 200, terminal payment including any failure code and ledger UUID | 404 `payment_not_found` |
| `check_payment_by_key(idempotency_key)` | `GET /payments/by-key`, required key header | 200, same terminal payment representation | 404 `payment_not_found` |
| `get_transaction_history(account_id, offset=0, limit=50)` | `GET /accounts/{account_id}/transactions`, offset up to 2⁶³−1 and limit 1–100 | 200, newest-first postings and nullable next offset | 404 `account_not_found` |

The API returns 422 with validation details for invalid requests, and may
return 500 for unexpected errors. Offset pages can shift when writes arrive.
Account creation has no idempotency. Refunds, a dedicated health endpoint,
authentication, and keyless payment submission require backend support. For
local startup checks, Compose can be used with the existing OpenAPI URL.

### Run a simulator smoke test

Start the service using the Compose or native setup above. Install the client
with `python -m pip install -e '.[test]'` in a Python 3.12+ environment. Save
the example as a Python script and run it while the simulator is up. Set
`PAYMENT_SERVICE_URL` if the service is listening elsewhere.

```python
import asyncio
import os
from uuid import uuid4

from app.schemas import PaymentRequest
from experiments.payment_client import PaymentClient, RequestContext


async def main():
    url = os.getenv("PAYMENT_SERVICE_URL", "http://127.0.0.1:8000")
    async with PaymentClient(base_url=url) as client:
        source, recipient = await asyncio.gather(
            client.create_account(1000),
            client.create_account(),
        )
        key = uuid4().hex
        context = RequestContext(agent_id="smoke-test", purchase_id="demo-purchase")
        payload = PaymentRequest(
            source_account_id=source.id,
            destination_account_id=recipient.id,
            amount_cents=125,
        )
        paid = await client.pay(payload, idempotency_key=key, context=context)
        assert paid.ledger_transaction_id is not None
        assert await client.check_payment_by_key(key) == paid
        assert await client.check_payment_status(paid.id) == paid
        balances = await asyncio.gather(
            client.check_balance(source.id),
            client.check_balance(recipient.id),
        )
        assert [account.balance_cents for account in balances] == [875, 125]
        history = await client.get_transaction_history(source.id)
        assert history.entries[0].transaction_id == paid.ledger_transaction_id


asyncio.run(main())
```

Each smoke-test run creates two simulator accounts. To run deterministic
tests without a database:

```sh
python -m pytest -q tests/test_payment_client.py tests/test_validation.py
```

### Outcomes and request identity

- `InvalidClientRequest` is raised before sending invalid input.
- `PaymentRejected` means the API returned a documented business rejection
  with a payment UUID. The exception category distinguishes insufficient
  funds from missing accounts or recipient overflow.
- `UnknownPaymentOutcome` covers ambiguous payment responses, including
  timeouts, connection failures, 5xx responses and malformed success bodies.
  `UnknownWriteOutcome` also covers ambiguous account creation. The
  `category` field retains the communication failure. Cancellation propagates
  as `asyncio.CancelledError` and logs a write outcome of `unknown`.
- `PaymentClientError` covers read failures, backend validation and key
  conflicts. A key conflict says the key belongs to a different payload. A
  lookup returning 404 says there was no visible row at the time of the read.
  A delayed or in-flight payment could still commit afterward.

Each call sends a fresh `X-Request-ID`. A caller can explicitly look up the
payment by key or resubmit the same payload and key after an ambiguous
response. The backend scopes keys globally and persists them indefinitely.
A different key can result in a separate transfer for the same logical
purchase. `RequestContext` keeps the experiment, agent and purchase IDs
local to the client. The payment UUID identifies the payment record and the
ledger UUID identifies its financial posting.

### Connections, logging and transport

Share one client across tasks on the same event loop and wait for the tasks
to finish before closing it. The owned HTTPX client defaults to five-second
timeouts for each of connect, read, write and pool acquisition, with 100
connections and 20 keep-alive connections. Pass an `httpx.Timeout` to set
individual limits and an `httpx.Limits` to tune the pool. Calls disable
redirects. The owned client ignores ambient proxy configuration and uses
HTTPX's default zero transport retries. An injected transport or caller-owned
HTTP client can change delivery behavior. The caller closes a supplied
`httpx.AsyncClient` and can configure its auth if a deployment requires it.

Configure Python logging at INFO for `experiments.payment_client` or supply a
logger. The `payment_service_request` record includes structured fields for
correlation, UTC start and finish times, elapsed milliseconds from a monotonic
clock, response status, payment and ledger IDs when observed, and the
client-observed outcome. It logs a SHA-256 digest of the idempotency key and
omits headers, request bodies and credentials.

A future fault injector can wrap an `httpx.AsyncBaseTransport` and own the
wrapped transport's cleanup. It can fail before forwarding, forward then
drop a reply, deliver twice or delay either side. Use ordinary HTTP
responses or HTTPX transport exceptions at that boundary so the client sees
the same signals it would see from the actual service. Keep fault-event
records in the experiment harness.
