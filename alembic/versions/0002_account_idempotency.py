"""Store the original response for keyed account creation."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE accounts ADD COLUMN creation_key VARCHAR(128) UNIQUE,
            ADD COLUMN opening_cents BIGINT,
            ADD CONSTRAINT account_creation_payload CHECK (
                (creation_key IS NULL AND opening_cents IS NULL)
                OR (creation_key IS NOT NULL AND opening_cents IS NOT NULL
                    AND opening_cents >= 0 AND NOT is_issuance)
            )
    """)


def downgrade():
    op.execute("ALTER TABLE accounts DROP COLUMN creation_key, DROP COLUMN opening_cents")
