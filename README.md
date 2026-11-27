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

The API assumes a trusted local simulator client. It has no authentication or purchase-level authorization. Idempotency keys are global and retained indefinitely. Different keys represent separate payment requests. Account creation accepts an optional `Idempotency-Key` in a separate namespace. A matching retry returns HTTP 200 with the original account ID and opening balance. Changing the opening amount returns HTTP 409. Unkeyed requests create a new account on each submission.

The issuance row serializes funded account creation and imposes a total issuance cap of 2^63 cents. Offset-based history pagination can shift if postings arrive between page reads. The API stores balances for efficient reads and checks, so direct SQL changes outside the service can bypass balance-to-ledger reconciliation. The local migration role can also change schema protections. Treat direct database writers and the migration role as trusted.

Agent adapters, fault injection, behavioral invariant checks, and experiment execution are outside Phase 1.
