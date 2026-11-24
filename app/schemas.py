from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_CENTS = 2**63 - 1
MIN_CENTS = -(2**63)
OpeningCents = Annotated[int, Field(strict=True, ge=0, le=MAX_CENTS)]
TransferCents = Annotated[int, Field(strict=True, gt=0, le=MAX_CENTS)]


class AccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opening_balance_cents: OpeningCents = 0


class AccountBalance(BaseModel):
    id: UUID
    balance_cents: int


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_account_id: UUID
    destination_account_id: UUID
    amount_cents: TransferCents

    @model_validator(mode="after")
    def distinct_accounts(self) -> Self:
        if self.source_account_id == self.destination_account_id:
            raise ValueError("Source and destination must be different accounts")
        return self


class Payment(BaseModel):
    id: UUID
    source_account_id: UUID
    destination_account_id: UUID
    amount_cents: int
    status: Literal["succeeded", "rejected"]
    failure_code: str | None
    created_at: datetime
    ledger_transaction_id: UUID | None


class Posting(BaseModel):
    transaction_id: UUID
    payment_id: UUID | None
    delta_cents: int
    created_at: datetime


class History(BaseModel):
    entries: list[Posting]
    next_offset: int | None
