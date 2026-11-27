from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.payment_client import PaymentClient
from agent.provider import Call


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Key(Arguments):
    idempotency_key: Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[!-~]+$")]


class MakePayment(Key):
    recipient: UUID
    amount_cents: Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class Status(Arguments):
    payment_id: UUID


class HistoryPage(Arguments):
    offset: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)] = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50


TOOLS = {
    "make_payment": (MakePayment, "Pay a simulator recipient in integer cents using an idempotency key."),
    "check_payment_status": (Status, "Get an authoritative outcome by payment UUID."),
    "lookup_payment_by_key": (Key, "Get an authoritative outcome by idempotency key."),
    "get_balance": (Arguments, "Read your bound simulator account balance."),
    "get_transaction_history": (HistoryPage, "Read a page of postings for your bound account."),
}


def definitions() -> list[dict]:
    return [{"name": name, "description": description, "parameters": schema.model_json_schema()}
            for name, (schema, description) in TOOLS.items()]


async def execute(client: PaymentClient, source: UUID, call: Call, invocation_id: str) -> dict:
    entry = TOOLS.get(call.name)
    if entry is None:
        return {"ok": False, "error": {"code": "unknown_tool"}}
    try:
        args = entry[0].model_validate(call.arguments)
    except ValidationError as exc:
        return {"ok": False, "error": {"code": "invalid_arguments", "details": [
            {"location": list(item["loc"]), "type": item["type"]} for item in exc.errors()
        ]}}
    match call.name:
        case "make_payment":
            return await client.request("POST", "/payments", invocation_id, source=source,
                                        key=args.idempotency_key, body={
                                            "source_account_id": str(source),
                                            "destination_account_id": str(args.recipient),
                                            "amount_cents": args.amount_cents,
                                        })
        case "check_payment_status":
            return await client.request("GET", f"/payments/{args.payment_id}", invocation_id,
                                        source=source, check_owner=True)
        case "lookup_payment_by_key":
            return await client.request("GET", "/payments/by-key", invocation_id, source=source,
                                        key=args.idempotency_key, check_owner=True)
        case "get_balance":
            return await client.request("GET", f"/accounts/{source}/balance", invocation_id, source=source)
        case "get_transaction_history":
            return await client.request("GET", f"/accounts/{source}/transactions", invocation_id,
                                        source=source, params=args.model_dump())
    raise AssertionError("Tool registry and dispatch disagree")
