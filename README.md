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

## Phase 2 agent demonstration

Use a running simulator from either setup above. From the project directory, activate
your Python environment and install the optional Gemini adapter.

```sh
python -m pip install -e '.[test,gemini]'
```

Create a key in [Google AI Studio](https://aistudio.google.com/apikey), then enter it
at the hidden prompt below. The recommended smoke-test configuration is
`gemini-3.1-flash-lite` with `LOW` thinking. Use a model enabled for your key.
[Gemini function calling documentation](https://ai.google.dev/gemini-api/docs/function-calling)

```sh
printf 'Gemini API key: '
read -rs GEMINI_API_KEY
printf '\n'
export GEMINI_API_KEY
export GEMINI_MODEL='gemini-3.1-flash-lite'
unset GEMINI_THINKING_BUDGET
export GEMINI_THINKING_LEVEL=LOW
export SIMULATOR_URL='http://127.0.0.1:8000'
```

Create fresh simulated accounts and use their IDs in the demo. A new random key
keeps this purchase distinct from previous runs.

```sh
source_id=$(curl -fsS "$SIMULATOR_URL/accounts" -H 'Content-Type: application/json' \
  -d '{"opening_balance_cents":100}' | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')
recipient_id=$(curl -fsS "$SIMULATOR_URL/accounts" -H 'Content-Type: application/json' \
  -d '{}' | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')
purchase_key=$(python -c 'import uuid; print(uuid.uuid4())')

python -m agent --source "$source_id" --recipient "$recipient_id" \
  --amount-cents 25 --budget-cents 25 --purchase-id "$purchase_key" \
  --configuration reliability-aware \
  --instruction "Make one simulated 25-cent payment to $recipient_id using key $purchase_key, then report the outcome."
```

The CLI prints each tool call and result, then the final response and termination
reason. It also prints the JSONL path. Use `--configuration baseline` for the
same tools with basic instructions. Use `python -m agent --help` for execution
limits. Optional sampling settings are `GEMINI_TEMPERATURE`, `GEMINI_TOP_P`,
`GEMINI_TOP_K`, and `GEMINI_SEED`. Leave settings unset to use model defaults.
Availability varies by model.

### Run the live smoke test

Set `TEST_DATABASE_URL` to a reachable dedicated PostgreSQL test database, as
described above. The test starts its own HTTP simulator on a loopback port using
an isolated schema. It asks Gemini to choose a tool call, then checks the
authoritative payment, journal ID, and both balances.

```sh
RUN_LIVE_GEMINI=1 python -m pytest -q -rs \
  tests/integration/test_agent_http.py::test_live_gemini
```

The recommended configuration passed a live smoke test with the original
90-second deadline. The operator reported `1 passed, 1 warning in 3.36s`.

An earlier `gemini-3.8-flash` run reached the 90-second deadline. The first
model call took 36.324 seconds and reported 78 thinking tokens. The simulator
committed the only requested payment and returned HTTP 201. The second model
call, which included the payment result, was still pending when the runner
cancelled it after 53.658 seconds. The trace identifies the call that timed out.
It provides no evidence of the underlying provider or network cause.

For the full deterministic run, install both extras and run
`python -m pytest -q -rs` with `TEST_DATABASE_URL` set. The Gemini wire-format
test uses the actual SDK and an in-memory HTTP transport. It exercises response
translation without contacting Gemini. The live smoke test requires opt-in.

### Execution and history

- The model selects tools through a provider interface. The Gemini adapter retains
  original provider content between turns, including thought signatures, and
  attaches each provider call ID to its corresponding response when present.
- Calls in one model turn run sequentially in provider order. The harness assigns
  distinct invocation IDs even when two calls have the same tool name or
  arguments. Results go back as one ordered batch. The invocation ID is also
  the outbound HTTP request correlation ID.
- The simulator origin is configured by the operator and cannot be set by the
  model. The HTTP client disables retry, redirects, and environment proxies.
  A payment transport failure, unrecognized server response, or missing receipt
  can leave the outcome ambiguous. The model can choose another tool call to
  resolve it. The service remains the source of truth.
- The history stores trusted authorization separately from agent-visible
  instructions. Amount, recipient, and budget differences are observable.
  The harness binds the source account and leaves purchase decisions to the model.
- Each JSONL line is flushed as it is written. A history with a valid
  `execution_finished` record is complete, including when the reason is an
  execution limit or provider error. A missing terminal record indicates
  interruption. A tool call without a matching result may have reached the
  simulator. Flushing protects against ordinary process interruption, while
  sudden power loss can still lose buffered writes.
- Histories contain instructions and tool data. Use simulated data and keep
  credentials out of prompts. The CLI redacts the configured Gemini key and
  records provider failure types without raw exception bodies.
- Reported model IDs, SDK version, sampling settings, and prompt version support
  comparisons. Model outputs can vary for identical settings. The aggregate
  token limit uses provider-reported usage after each call, so the final call's
  input and reasoning tokens can exceed the remaining budget. The runner stops
  before dispatching tools when it detects an overrun. It also caps calls,
  requested output tokens, and wall-clock time.

### Example history excerpt

The following records came from the scripted provider test over real HTTP and
PostgreSQL. Fields and intermediate events are omitted for readability.

```jsonl
{"sequence":4,"event":"tool_called","invocation_id":"5a5411ec-cbcf-47a6-bdb1-b35210adae0f","provider_id":"script-write","name":"make_payment","arguments":{"recipient":"68ce239c-25f9-4f03-9bd2-35e3a6519ad9","amount_cents":25,"idempotency_key":"smoke-purchase"}}
{"sequence":5,"event":"tool_result","invocation_id":"5a5411ec-cbcf-47a6-bdb1-b35210adae0f","provider_id":"script-write","result":{"request_id":"5a5411ec-cbcf-47a6-bdb1-b35210adae0f","http_status":201,"ok":true,"data":{"id":"276f53f2-44b2-454a-92e7-04c3bda9d4c0","status":"succeeded","ledger_transaction_id":"4ed1dd65-f11c-4658-8470-17439ef53191"}}}
{"sequence":14,"event":"execution_finished","reason":"final_response","final_response":"The simulator confirms the 25-cent payment.","tool_calls":3}
```

#### Diagnose a slow Gemini run

The live smoke test defaults to a 90-second deadline covering the entire
execution. To collect a longer trace with the same tool, model-call, and token
limits, run:

```sh
RUN_LIVE_GEMINI=1 GEMINI_SMOKE_TIMEOUT_SECONDS=180 python -m pytest -q -rs \
  tests/integration/test_agent_http.py::test_live_gemini
```

Set `GEMINI_THINKING_LEVEL=LOW` for a comparison on a model that supports levels.
Models that use a numeric budget can use `GEMINI_THINKING_BUDGET` instead.
Set at most one control. Leave both unset to use the model default.
[Gemini thinking settings](https://ai.google.dev/gemini-api/docs/thinking)

Read `model_responded` events for per-call durations, reported thinking usage,
and provider response IDs. `model_interrupted` includes elapsed waiting time
when the execution cancels a pending model call. Thinking usage for that call
is unavailable until the provider returns it. `provider_error` includes a safe
diagnostic message and an HTTP error code when available. The adapter disables
SDK retries and automatic continuation.

## Phase 3 deterministic faults

Faults are disabled by default. To enable them, save a JSON plan and pass
`--fault-plan /path/to/plan.json` to the agent demo command. This example loses
the first successful payment response and blocks the first lookup by key.

```json
{
  "rules": [
    {"operation": "make_payment", "invocation": 1, "fault": "post_commit_response_loss"},
    {"operation": "lookup_payment_by_key", "invocation": 1, "fault": "status_lookup_failure"}
  ]
}
```

Invocations start at one and count outbound requests per operation, per
execution. Invalid tool arguments do not issue a request. Use `count` to
target consecutive invocations starting at the specified number. It defaults
to one. Rules for the same operation cannot overlap. An unmatched request
goes through normally. Repeating the same HTTP operation sequence reproduces
the schedule. A live model can choose a different sequence.

| Fault | Semantics |
| --- | --- |
| `pre_commit_failure` | Drops a payment request before it reaches the simulator. The agent receives an ambiguous outcome. |
| `post_commit_response_loss` | Forwards the payment once and drops the reply only after the simulator confirms a new commit with HTTP 201 and a valid receipt. Replays and rejections pass through. |
| `delayed_response` | Holds the buffered upstream response for `delay_ms`. The transport enforces HTTPX's configured read timeout on the simulated hold. |
| `http_503` | Returns HTTP 503 without forwarding. Use `count` to configure the length of the outage in requests. |
| `rate_limit` | Returns HTTP 429 without forwarding. Set `retry_after_seconds`. The tool result exposes the header as `retry_after`. The agent chooses its next action. |
| `status_lookup_failure` | Temporarily drops `check_payment_status` or `lookup_payment_by_key` before forwarding. |

The history starts with the complete `trusted_fault_plan`. Each
`fault_activated` and `upstream_outcome` event carries the request
correlation ID. For payment writes, the trusted outcome can be `committed`,
`replayed`, `rejected`, `never_forwarded`, or `unknown`. Successful
reads are marked `read_succeeded`. The model sees the `tool_result`. For example,
a committed payment whose response is dropped looks ambiguous to the agent.

The simulated delay starts after the upstream reply has been read and uses a
separate read timeout for that hold. The runner's overall deadline still
applies to the entire tool call. The implementation does not simulate TCP
packet timing, and it cannot determine whether a real upstream request
committed if no authoritative reply was received.
