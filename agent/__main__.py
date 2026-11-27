import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from agent.config import Limits, PROMPTS
from agent.history import History
from agent.payment_client import PaymentClient
from agent.runner import Execution, Intent, run


def arguments():
    parser = argparse.ArgumentParser(description="Run a Gemini agent against a payment simulator.")
    parser.add_argument("--source", required=True, type=UUID)
    parser.add_argument("--recipient", required=True, type=UUID)
    parser.add_argument("--amount-cents", required=True, type=int)
    parser.add_argument("--budget-cents", required=True, type=int)
    parser.add_argument("--purchase-id", default="demo-purchase")
    parser.add_argument("--experiment-id", type=UUID, default=uuid4())
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--configuration", choices=PROMPTS, default="baseline")
    parser.add_argument("--history", type=Path, default=Path(f"agent-{uuid4()}.jsonl"))
    parser.add_argument("--max-tool-calls", type=int, default=12)
    parser.add_argument("--max-model-calls", type=int, default=8)
    parser.add_argument("--max-seconds", type=float, default=60)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--max-total-tokens", type=int, default=24000)
    return parser.parse_args()


def observe(record):
    if record["event"] in {"tool_called", "tool_result", "tool_interrupted", "tool_skipped",
                           "execution_finished", "provider_error", "model_interrupted"}:
        print(json.dumps(record), flush=True)


async def demo(args):
    from agent.providers.gemini import Gemini, thinking_from_env

    api_key = os.environ["GEMINI_API_KEY"]
    model_name = os.environ["GEMINI_MODEL"]
    sampling = {
        name: convert(os.environ[env]) if env in os.environ else None
        for name, env, convert in [
            ("temperature", "GEMINI_TEMPERATURE", float), ("top_p", "GEMINI_TOP_P", float),
            ("top_k", "GEMINI_TOP_K", int), ("seed", "GEMINI_SEED", int),
        ]
    }
    context = Execution(args.experiment_id, args.source, args.instruction,
                        Intent(args.purchase_id, args.recipient, args.amount_cents, args.budget_cents),
                        args.configuration)
    limits = Limits(args.max_tool_calls, args.max_model_calls, args.max_seconds,
                    args.max_output_tokens, args.max_total_tokens)
    async with Gemini(api_key=api_key, model=model_name, **sampling, **thinking_from_env()) as provider, PaymentClient(
        os.getenv("SIMULATOR_URL", "http://127.0.0.1:8000")
    ) as client:
        with History(args.history, secrets=(api_key,), observer=observe) as history:
            print(f"History at {args.history}", flush=True)
            return await run(provider, client, context, history, limits)


def main():
    args = arguments()
    try:
        result = asyncio.run(demo(args))
    except (KeyError, ValueError, OSError, ImportError) as exc:
        raise SystemExit(f"Unable to start demonstration ({type(exc).__name__}). Check README setup.") from None
    raise SystemExit(0 if result["reason"] == "final_response" else 1)


if __name__ == "__main__":
    main()
