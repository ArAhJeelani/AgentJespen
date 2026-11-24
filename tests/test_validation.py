from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas import MAX_CENTS, AccountRequest, PaymentRequest


@pytest.mark.parametrize("amount", [0, -1, 1.0, "1", True, MAX_CENTS + 1])
def test_transfer_requires_positive_integer_cents(amount):
    with pytest.raises(ValidationError):
        PaymentRequest(source_account_id=uuid4(), destination_account_id=uuid4(), amount_cents=amount)


@pytest.mark.parametrize("amount", [-1, 1.0, "1", False, MAX_CENTS + 1])
def test_opening_balance_requires_nonnegative_integer_cents(amount):
    with pytest.raises(ValidationError):
        AccountRequest(opening_balance_cents=amount)


def test_exact_bigint_limit_is_accepted():
    assert AccountRequest(opening_balance_cents=MAX_CENTS).opening_balance_cents == MAX_CENTS
    assert PaymentRequest(
        source_account_id=uuid4(), destination_account_id=uuid4(), amount_cents=MAX_CENTS
    ).amount_cents == MAX_CENTS
