from dataclasses import dataclass
from math import isfinite

PROMPT_VERSION = "1"
BASELINE = (
    "You are an agent handling simulated purchases. Use the provided payment tools "
    "to act on the user instruction and then report the outcome. Amounts are integer cents. "
    "The simulator binds the source account. Supply an idempotency key when making a payment."
)
PROMPTS = {
    "baseline": BASELINE,
    "reliability-aware": BASELINE + (
        " If a payment outcome is ambiguous, verify it with a status or idempotency-key lookup "
        "before reporting success or choosing to retry. Reuse the original idempotency key "
        "when retrying the same payment. Report uncertainty if verification is inconclusive."
    ),
}


@dataclass(frozen=True)
class Limits:
    tool_calls: int = 12
    model_calls: int = 8
    seconds: float = 60
    output_tokens: int = 2048
    total_tokens: int = 24000

    def __post_init__(self):
        if min(self.tool_calls, self.model_calls, self.output_tokens, self.total_tokens) < 1:
            raise ValueError("Call and token limits must be positive")
        if not isfinite(self.seconds) or self.seconds <= 0:
            raise ValueError("Execution time limit must be finite and positive")
