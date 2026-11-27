from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.schemas import MAX_CENTS, MIN_CENTS, PaymentRequest

ISSUANCE_ID = UUID(int=0)

ERRORS = {
    "account_not_found": (404, "Customer account does not exist"),
    "payment_not_found": (404, "Payment does not exist"),
    "source_not_found": (404, "Source account does not exist"),
    "recipient_not_found": (404, "Recipient account does not exist"),
    "insufficient_funds": (409, "Source account has insufficient funds"),
    "balance_limit": (409, "Recipient balance would exceed the supported range"),
    "issuance_limit": (409, "Opening balance would exceed the issuance account's range"),
    "idempotency_conflict": (409, "This idempotency key belongs to a different request payload"),
}


class PaymentError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(ERRORS[code][1])


def post_transfer(connection: Connection, source: UUID, destination: UUID,
                  amount: int, request_id: str, payment_id: UUID | None = None) -> UUID:
    # Callers hold both account locks, or own the newly inserted destination.
    transaction_id = uuid4()
    connection.execute(text("""
        UPDATE accounts SET balance_cents = balance_cents + :delta WHERE id = :id
    """), [{"id": source, "delta": -amount}, {"id": destination, "delta": amount}])
    connection.execute(text("""
        INSERT INTO ledger_transactions (id, payment_id, request_id)
        VALUES (:id, :payment_id, :request_id)
    """), {"id": transaction_id, "payment_id": payment_id, "request_id": request_id})
    connection.execute(text("""
        INSERT INTO ledger_entries (transaction_id, account_id, delta_cents)
        VALUES (:transaction_id, :account_id, :delta)
    """), [
        {"transaction_id": transaction_id, "account_id": source, "delta": -amount},
        {"transaction_id": transaction_id, "account_id": destination, "delta": amount},
    ])
    return transaction_id


def create_account(engine: Engine, amount: int, request_id: str, key: str | None = None):
    account_id = uuid4()
    transaction_id = None
    with engine.begin() as connection:
        inserted = connection.execute(text("""
            INSERT INTO accounts (id, creation_key, opening_cents)
            VALUES (:id, :key, :opening)
            ON CONFLICT (creation_key) DO NOTHING RETURNING id
        """), {"id": account_id, "key": key, "opening": amount if key is not None else None})
        if inserted.scalar_one_or_none() is None:
            original = connection.execute(text("""
                SELECT id, opening_cents FROM accounts WHERE creation_key = :key
            """), {"key": key}).one()
            if original.opening_cents != amount:
                raise PaymentError("idempotency_conflict")
            return {"id": original.id, "balance_cents": original.opening_cents}, None, True
        if amount:
            issuance = connection.execute(text("""
                SELECT balance_cents FROM accounts WHERE id = :id FOR UPDATE
            """), {"id": ISSUANCE_ID}).scalar_one()
            if issuance - amount < MIN_CENTS:
                raise PaymentError("issuance_limit")
            transaction_id = post_transfer(connection, ISSUANCE_ID, account_id, amount, request_id)
    return {"id": account_id, "balance_cents": amount}, transaction_id, False


def get_account(connection: Connection, account_id: UUID):
    account = connection.execute(text("""
        SELECT id, balance_cents FROM accounts WHERE id = :id AND NOT is_issuance
    """), {"id": account_id}).mappings().one_or_none()
    if account is None:
        raise PaymentError("account_not_found")
    return account


def find_payment(connection: Connection, *, key: str | None = None, payment_id: UUID | None = None):
    predicate = "p.idempotency_key = :value" if key is not None else "p.id = :value"
    payment = connection.execute(text(f"""
        SELECT p.id, p.source_account_id, p.destination_account_id, p.amount_cents,
               p.status, p.failure_code, p.created_at, l.id AS ledger_transaction_id
        FROM payments p LEFT JOIN ledger_transactions l ON l.payment_id = p.id
        WHERE {predicate}
    """), {"value": key if key is not None else payment_id}).mappings().one_or_none()
    if payment is None:
        raise PaymentError("payment_not_found")
    return dict(payment)


def rejection(balances: dict, source: UUID, destination: UUID, amount: int) -> str | None:
    if source not in balances:
        return "source_not_found"
    if destination not in balances:
        return "recipient_not_found"
    if balances[source] < amount:
        return "insufficient_funds"
    if balances[destination] + amount > MAX_CENTS:
        return "balance_limit"
    return None


def create_payment(engine: Engine, payload: PaymentRequest, key: str, request_id: str):
    parameters = payload.model_dump()
    source, destination, amount = (
        payload.source_account_id, payload.destination_account_id, payload.amount_cents
    )
    with engine.begin() as connection:
        payment_id = connection.execute(text("""
            INSERT INTO payments
                (id, idempotency_key, source_account_id, destination_account_id, amount_cents, request_id)
            VALUES (:id, :key, :source_account_id, :destination_account_id, :amount_cents, :request_id)
            ON CONFLICT (idempotency_key) DO NOTHING RETURNING id
        """), parameters | {"id": uuid4(), "key": key, "request_id": request_id}).scalar_one_or_none()
        if payment_id is None:
            # A new READ COMMITTED snapshot can see the winning transaction.
            payment = find_payment(connection, key=key)
            if any(payment[field] != value for field, value in parameters.items()):
                raise PaymentError("idempotency_conflict")
            return payment, True

        rows = connection.execute(text("""
            SELECT id, balance_cents FROM accounts
            WHERE id IN (:source, :destination) AND NOT is_issuance
            ORDER BY id FOR UPDATE
        """), {"source": source, "destination": destination}).all()
        failure = rejection({row.id: row.balance_cents for row in rows}, source, destination, amount)
        connection.execute(text("""
            UPDATE payments SET status = :status, failure_code = :failure WHERE id = :id
        """), {"id": payment_id, "status": "rejected" if failure else "succeeded", "failure": failure})
        if failure is None:
            post_transfer(connection, source, destination, amount, request_id, payment_id)
        payment = find_payment(connection, payment_id=payment_id)
    return payment, False


def account_history(connection: Connection, account_id: UUID, offset: int, limit: int):
    get_account(connection, account_id)
    rows = connection.execute(text("""
        SELECT l.id AS transaction_id, l.payment_id, e.delta_cents, l.created_at
        FROM ledger_entries e JOIN ledger_transactions l ON l.id = e.transaction_id
        WHERE e.account_id = :id
        ORDER BY l.created_at DESC, l.id DESC
        OFFSET :offset LIMIT :count
    """), {"id": account_id, "offset": offset, "count": limit + 1}).mappings().all()
    return {"entries": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None}
