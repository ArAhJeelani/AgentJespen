import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from app.main import logger
from app.payments import ISSUANCE_ID
from app.schemas import MAX_CENTS, MIN_CENTS


def account(client, amount=0):
    response = client.post("/accounts", json={"opening_balance_cents": amount})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def pay(client, source, destination, amount, key="purchase", request_id=None):
    headers = {"Idempotency-Key": key}
    if request_id:
        headers["X-Request-ID"] = request_id
    return client.post("/payments", headers=headers, json={
        "source_account_id": source, "destination_account_id": destination, "amount_cents": amount,
    })


def by_key(client, key="purchase"):
    return client.get("/payments/by-key", headers={"Idempotency-Key": key})


def balance(client, account_id):
    response = client.get(f"/accounts/{account_id}/balance")
    assert response.status_code == 200, response.text
    return response.json()["balance_cents"]


def snapshot(engine):
    with engine.connect() as connection:
        balances = dict(connection.execute(text("SELECT id, balance_cents FROM accounts")).tuples().all())
        counts = tuple(connection.execute(text("""
            SELECT (SELECT count(*) FROM payments), (SELECT count(*) FROM ledger_transactions),
                   (SELECT count(*) FROM ledger_entries)
        """)).one())
    return balances, counts


def assert_reconciled(engine):
    with engine.connect() as connection:
        accounts = connection.execute(text("""
            SELECT a.id, a.balance_cents, COALESCE(sum(e.delta_cents), 0) AS ledger_balance
            FROM accounts a LEFT JOIN ledger_entries e ON e.account_id = a.id GROUP BY a.id
        """)).all()
        assert sum(row.balance_cents for row in accounts) == 0
        assert all(row.balance_cents == row.ledger_balance for row in accounts)
        assert all(row.balance_cents >= 0 for row in accounts if row.id != ISSUANCE_ID)
        totals = connection.execute(text("""
            SELECT count(e.account_id), sum(e.delta_cents)
            FROM ledger_transactions l LEFT JOIN ledger_entries e ON e.transaction_id = l.id
            GROUP BY l.id
        """)).tuples().all()
        assert all(count == 2 and total == 0 for count, total in totals)


def race_at(engine, statement_fragment, *operations):
    """Hold each worker at the same SQL step and prove it has its own backend."""
    barrier = Barrier(len(operations), timeout=8)
    mutex = Lock()
    backends = set()

    def synchronize(connection, cursor, statement, parameters, context, executemany):
        if statement_fragment in statement:
            with mutex:
                backends.add(connection.connection.driver_connection.info.backend_pid)
            barrier.wait()

    event.listen(engine, "before_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            futures = [pool.submit(operation) for operation in operations]
            results = [future.result(timeout=25) for future in futures]
        assert len(backends) == len(operations)
        return results
    finally:
        event.remove(engine, "before_cursor_execute", synchronize)


def test_transfer_history_lookup_and_correlation(client, engine, caplog):
    source, recipient = account(client, 100), account(client)
    logger.addHandler(caplog.handler)
    try:
        result = pay(client, source, recipient, 30, request_id="run-42.attempt-1")
        replay = pay(client, source, recipient, 30, request_id="run-42.attempt-2")
    finally:
        logger.removeHandler(caplog.handler)
    assert result.status_code == 201
    assert replay.status_code == 200
    assert result.headers["X-Request-ID"] == "run-42.attempt-1"
    payment = result.json()
    assert replay.json() == payment == by_key(client).json()
    assert client.get(f"/payments/{payment['id']}").json() == payment
    assert payment["status"] == "succeeded"
    assert (balance(client, source), balance(client, recipient)) == (70, 30)
    first = client.get(f"/accounts/{source}/transactions?limit=1").json()
    assert first["next_offset"] == 1
    assert first["entries"][0]["delta_cents"] == -30
    assert first["entries"][0]["transaction_id"] == payment["ledger_transaction_id"]
    second = client.get(f"/accounts/{source}/transactions?limit=1&offset=1").json()
    assert second["next_offset"] is None
    assert second["entries"][0]["delta_cents"] == 100
    assert second["entries"][0]["payment_id"] is None
    credit = client.get(f"/accounts/{recipient}/transactions").json()["entries"][0]
    assert credit["delta_cents"] == 30 and credit["payment_id"] == payment["id"]
    events = [json.loads(rec.message) for rec in caplog.records if rec.name == logger.name]
    committed = [evt for evt in events if evt["event"] == "payment_committed"]
    assert len(committed) == 1 and committed[0]["request_id"] == "run-42.attempt-1"
    assert committed[0]["payment_id"] == payment["id"]
    assert committed[0]["ledger_transaction_id"] == payment["ledger_transaction_id"]
    assert [evt["request_id"] for evt in events if evt["event"] == "payment_replayed"] == ["run-42.attempt-2"]
    with engine.connect() as connection:
        request_ids = connection.execute(text("""
            SELECT p.request_id, l.request_id FROM payments p JOIN ledger_transactions l ON l.payment_id = p.id
        """)).one()
    assert tuple(request_ids) == ("run-42.attempt-1", "run-42.attempt-1")
    assert snapshot(engine)[1] == (1, 2, 4)
    assert_reconciled(engine)


def test_insufficient_funds_rejection_survives_funding(client, engine):
    source, recipient = account(client), account(client)
    rejected = pay(client, source, recipient, 5)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "insufficient_funds"
    payment = by_key(client).json()
    assert payment["status"] == "rejected" and payment["ledger_transaction_id"] is None
    assert payment["id"] == rejected.json()["error"]["payment_id"]
    funder = account(client, 5)
    assert pay(client, funder, source, 5, "fund").status_code == 201
    replay = pay(client, source, recipient, 5)
    assert replay.status_code == 409 and replay.json()["error"]["payment_id"] == payment["id"]
    assert client.get(f"/payments/{payment['id']}").json() == payment
    assert (balance(client, source), balance(client, recipient)) == (5, 0)
    assert_reconciled(engine)


@pytest.mark.parametrize("missing", ["source", "recipient", "issuance_as_recipient"])
def test_unknown_account_rejection_has_stable_id(client, engine, missing):
    existing = account(client, 10)
    absent = str(ISSUANCE_ID if missing == "issuance_as_recipient" else uuid4())
    source, recipient = (absent, existing) if missing == "source" else (existing, absent)
    code = "source_not_found" if missing == "source" else "recipient_not_found"
    for _ in range(2):
        response = pay(client, source, recipient, 1)
        assert response.status_code == 404 and response.json()["error"]["code"] == code
        assert by_key(client).json()["id"] == response.json()["error"]["payment_id"]
    assert snapshot(engine)[1] == (1, 1, 2)
    assert_reconciled(engine)


def test_validation_does_not_consume_key(client, engine):
    source, recipient = account(client, 10), account(client)
    for destination, amount in [(recipient, 0), (recipient, 1.0), ("bad", 1), (source, 1)]:
        response = pay(client, source, destination, amount, request_id="validation")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"
        assert response.headers["X-Request-ID"] == response.json()["error"]["request_id"] == "validation"
    assert by_key(client).status_code == 404
    assert pay(client, source, recipient, 1).status_code == 201
    assert snapshot(engine)[1] == (1, 2, 4)


def test_conflicting_payload(client, engine):
    source, recipient, other = account(client, 10), account(client), account(client)
    original = pay(client, source, recipient, 5)
    assert original.status_code == 201
    for args in [(source, recipient, 6), (source, other, 5), (other, recipient, 5)]:
        response = pay(client, *args)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "idempotency_conflict"
    assert by_key(client).json() == original.json()
    assert snapshot(engine)[1] == (1, 2, 4)
    assert_reconciled(engine)


@pytest.mark.parametrize("conflicting", [False, True])
def test_concurrent_key_claim(client, engine, conflicting):
    source, recipient = account(client, 100), account(client)
    first, second = race_at(
        engine, "INSERT INTO payments",
        lambda: pay(client, source, recipient, 30),
        lambda: pay(client, source, recipient, 40 if conflicting else 30),
    )
    winner = next(response for response in (first, second) if response.status_code == 201)
    loser = second if winner is first else first
    assert loser.status_code == (409 if conflicting else 200)
    if conflicting:
        assert loser.json()["error"]["code"] == "idempotency_conflict"
    else:
        assert loser.json() == winner.json()
    assert by_key(client).json() == winner.json()
    assert balance(client, source) == 100 - winner.json()["amount_cents"]
    assert balance(client, recipient) == winner.json()["amount_cents"]
    assert snapshot(engine)[1] == (1, 2, 4)
    assert_reconciled(engine)


def test_concurrent_withdrawals_cannot_overspend(client, engine):
    source, one, two = account(client, 100), account(client), account(client)
    responses = race_at(
        engine, "ORDER BY id FOR UPDATE",
        lambda: pay(client, source, one, 80, "one"),
        lambda: pay(client, source, two, 80, "two"),
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json()["error"]["code"] == "insufficient_funds"
    assert balance(client, source) == 20
    assert sorted([balance(client, one), balance(client, two)]) == [0, 80]
    assert snapshot(engine)[1] == (2, 2, 4)
    assert_reconciled(engine)


def test_opposing_transfers_use_consistent_lock_order(client, engine):
    left, right = account(client, 100), account(client, 100)
    results = race_at(
        engine, "ORDER BY id FOR UPDATE",
        lambda: pay(client, left, right, 10, "left"),
        lambda: pay(client, right, left, 20, "right"),
    )
    assert [result.status_code for result in results] == [201, 201]
    assert (balance(client, left), balance(client, right)) == (110, 90)
    assert_reconciled(engine)


@pytest.mark.parametrize("opening", [False, True])
def test_commit_failure_rolls_back_all_writes(client, engine, opening):
    source, recipient = account(client, 10), account(client)
    before = snapshot(engine)
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE FUNCTION test_drop_credit() RETURNS TRIGGER LANGUAGE plpgsql AS $$
            BEGIN IF NEW.delta_cents > 0 THEN RETURN NULL; END IF; RETURN NEW; END;
            $$
        """))
        connection.execute(text("""
            CREATE TRIGGER test_drop_credit BEFORE INSERT ON ledger_entries
            FOR EACH ROW EXECUTE FUNCTION test_drop_credit()
        """))
    headers = {"Idempotency-Key": "rollback-opening"}
    response = (client.post("/accounts", json={"opening_balance_cents": 5}, headers=headers)
                if opening else pay(client, source, recipient, 5))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert snapshot(engine) == before
    assert by_key(client).status_code == 404
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER test_drop_credit ON ledger_entries"))
    if opening:
        retry = client.post("/accounts", json={"opening_balance_cents": 5}, headers=headers)
        assert retry.status_code == 201
        assert balance(client, retry.json()["id"]) == 5
    else:
        assert pay(client, source, recipient, 5).status_code == 201
    assert_reconciled(engine)


def test_issuance_floor_is_enforced_under_concurrency(client, engine):
    account(client, MAX_CENTS)
    before = snapshot(engine)
    def open_last_cent():
        return client.post("/accounts", json={"opening_balance_cents": 1})

    results = race_at(engine, "WHERE id = %(id)s FOR UPDATE", open_last_cent, open_last_cent)
    assert sorted(result.status_code for result in results) == [201, 409]
    rejected = next(result for result in results if result.status_code == 409)
    assert rejected.json()["error"]["code"] == "issuance_limit"
    balances, counts = snapshot(engine)
    assert len(balances) == len(before[0]) + 1
    assert balances[ISSUANCE_ID] == MIN_CENTS
    assert counts == (0, 2, 4)
    assert_reconciled(engine)


def test_recipient_overflow_is_persisted_without_postings(client, engine):
    recipient, source = account(client, MAX_CENTS), account(client, 1)
    response = pay(client, source, recipient, 1)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "balance_limit"
    assert by_key(client).json()["failure_code"] == "balance_limit"
    assert (balance(client, source), balance(client, recipient)) == (1, MAX_CENTS)
    assert snapshot(engine)[1] == (1, 2, 4)
    assert_reconciled(engine)


def test_ledger_cannot_be_changed_or_extended_after_commit(client, engine):
    source, recipient, unrelated = account(client, 10), account(client), account(client)
    response = pay(client, source, recipient, 1)
    assert response.status_code == 201
    journal = response.json()["ledger_transaction_id"]
    before = snapshot(engine)
    for statement in [
        "UPDATE ledger_entries SET delta_cents = 7",
        "DELETE FROM ledger_entries",
        "UPDATE ledger_transactions SET created_at = now()",
        "DELETE FROM ledger_transactions",
        "TRUNCATE ledger_entries",
        "TRUNCATE ledger_transactions CASCADE",
    ]:
        with pytest.raises(IntegrityError) as exc, engine.begin() as connection:
            connection.execute(text(statement))
        assert exc.value.orig.sqlstate == "23514"
    for delta in [-1, 1]:
        with pytest.raises(IntegrityError) as exc, engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO ledger_entries (transaction_id, account_id, delta_cents)
                VALUES (:journal, :account, :delta)
            """), {"journal": journal, "account": unrelated, "delta": delta})
        assert exc.value.orig.sqlstate == "23505"
    assert snapshot(engine) == before
    assert_reconciled(engine)


def test_keyed_opening_replays_original_response(client, engine):
    headers = {"Idempotency-Key": "opening"}
    first = client.post("/accounts", json={"opening_balance_cents": 20}, headers=headers)
    assert first.status_code == 201
    source, recipient = first.json()["id"], account(client)
    assert pay(client, source, recipient, 5, "opening").status_code == 201
    replay = client.post("/accounts", json={"opening_balance_cents": 20}, headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    conflict = client.post("/accounts", json={"opening_balance_cents": 21}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert balance(client, source) == 15
    assert snapshot(engine)[1] == (1, 2, 4)
    assert_reconciled(engine)


def test_concurrent_keyed_opening_issues_once(client, engine):
    def open_account():
        return client.post("/accounts", json={"opening_balance_cents": 12},
                           headers={"Idempotency-Key": "concurrent-opening"})

    first, second = race_at(engine, "INSERT INTO accounts", open_account, open_account)
    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert first.json() == second.json()
    assert snapshot(engine)[1] == (0, 1, 2)
    assert_reconciled(engine)
