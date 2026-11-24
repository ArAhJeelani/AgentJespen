"""Create accounts, payment requests and the double-entry ledger."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE accounts (
            id UUID PRIMARY KEY,
            is_issuance BOOLEAN NOT NULL DEFAULT FALSE,
            balance_cents BIGINT NOT NULL DEFAULT 0,
            CHECK ((is_issuance AND balance_cents <= 0)
                OR (NOT is_issuance AND balance_cents >= 0))
        )
    """)
    op.execute("CREATE UNIQUE INDEX one_issuance_account ON accounts (is_issuance) WHERE is_issuance")
    op.execute("""
        INSERT INTO accounts (id, is_issuance)
        VALUES ('00000000-0000-0000-0000-000000000000', TRUE)
    """)
    op.execute("""
        CREATE TABLE payments (
            id UUID PRIMARY KEY,
            idempotency_key VARCHAR(128) NOT NULL UNIQUE,
            source_account_id UUID NOT NULL,
            destination_account_id UUID NOT NULL,
            amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
            status TEXT NOT NULL DEFAULT 'pending',
            failure_code TEXT,
            request_id VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CHECK (source_account_id <> destination_account_id),
            CHECK ((status IN ('pending', 'succeeded') AND failure_code IS NULL)
                OR (status = 'rejected' AND failure_code IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE TABLE ledger_transactions (
            id UUID PRIMARY KEY,
            payment_id UUID UNIQUE REFERENCES payments (id),
            request_id VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        )
    """)
    op.execute("""
        CREATE TABLE ledger_entries (
            transaction_id UUID NOT NULL REFERENCES ledger_transactions (id),
            account_id UUID NOT NULL REFERENCES accounts (id),
            delta_cents BIGINT NOT NULL CHECK (delta_cents <> 0),
            is_credit BOOLEAN GENERATED ALWAYS AS (delta_cents > 0) STORED,
            PRIMARY KEY (transaction_id, account_id),
            UNIQUE (transaction_id, is_credit)
        )
    """)
    op.execute("CREATE INDEX ledger_entries_by_account ON ledger_entries (account_id, transaction_id)")
    op.execute("""
        CREATE FUNCTION forbid_ledger_change() RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Posted ledger records are immutable' USING ERRCODE = '23514';
        END;
        $$
    """)
    for table in ("ledger_transactions", "ledger_entries"):
        op.execute(f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION forbid_ledger_change()
        """)
    op.execute("""
        CREATE FUNCTION check_ledger_transaction() RETURNS TRIGGER LANGUAGE plpgsql AS $$
        DECLARE
            debit ledger_entries%ROWTYPE;
            credit ledger_entries%ROWTYPE;
            payment payments%ROWTYPE;
        BEGIN
            SELECT * INTO debit FROM ledger_entries
                WHERE transaction_id = NEW.id AND NOT is_credit;
            SELECT * INTO credit FROM ledger_entries
                WHERE transaction_id = NEW.id AND is_credit;
            IF debit.account_id IS NULL OR credit.account_id IS NULL
                OR debit.delta_cents::numeric + credit.delta_cents <> 0 THEN
                RAISE EXCEPTION 'Ledger transaction requires two balanced postings'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.payment_id IS NULL THEN
                IF NOT (SELECT is_issuance FROM accounts WHERE id = debit.account_id) THEN
                    RAISE EXCEPTION 'Opening balance must debit the issuance account'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                SELECT * INTO payment FROM payments WHERE id = NEW.payment_id;
                IF payment.status <> 'succeeded'
                    OR payment.source_account_id <> debit.account_id
                    OR payment.destination_account_id <> credit.account_id
                    OR payment.amount_cents <> credit.delta_cents
                    OR EXISTS (
                        SELECT 1 FROM accounts WHERE is_issuance
                            AND id IN (debit.account_id, credit.account_id)
                    ) THEN
                    RAISE EXCEPTION 'Postings must match a successful customer payment'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER ledger_transaction_balanced
        AFTER INSERT ON ledger_transactions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION check_ledger_transaction()
    """)
    op.execute("""
        CREATE FUNCTION check_payment_finalized() RETURNS TRIGGER LANGUAGE plpgsql AS $$
        DECLARE
            final_status TEXT;
        BEGIN
            SELECT status INTO final_status FROM payments WHERE id = NEW.id;
            IF final_status = 'pending' OR (
                (final_status = 'succeeded') <> EXISTS (
                    SELECT 1 FROM ledger_transactions WHERE payment_id = NEW.id
                )
            ) THEN
                RAISE EXCEPTION 'Every payment needs a terminal outcome and matching ledger state'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END;
        $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER payment_finalized
        AFTER INSERT OR UPDATE ON payments DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION check_payment_finalized()
    """)
    op.execute("""
        CREATE FUNCTION protect_payment_outcome() RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR OLD.status <> 'pending'
                OR ROW(NEW.id, NEW.idempotency_key, NEW.source_account_id,
                       NEW.destination_account_id, NEW.amount_cents, NEW.request_id, NEW.created_at)
                IS DISTINCT FROM
                   ROW(OLD.id, OLD.idempotency_key, OLD.source_account_id,
                       OLD.destination_account_id, OLD.amount_cents, OLD.request_id, OLD.created_at) THEN
                RAISE EXCEPTION 'Payment requests and terminal outcomes are immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER payment_outcome_immutable
        BEFORE UPDATE OR DELETE ON payments
        FOR EACH ROW EXECUTE FUNCTION protect_payment_outcome()
    """)
    op.execute("""
        CREATE TRIGGER payments_no_truncate
        BEFORE TRUNCATE ON payments
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_ledger_change()
    """)


def downgrade():
    op.execute("DROP TABLE ledger_entries, ledger_transactions, payments, accounts")
    op.execute("""
        DROP FUNCTION protect_payment_outcome(), check_payment_finalized(),
            check_ledger_transaction(), forbid_ledger_change()
    """)
