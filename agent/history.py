import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


class History:
    def __init__(self, path: Path, *, secrets: tuple[str, ...] = (),
                 observer: Callable[[dict], None] | None = None):
        self.path = path
        self.secrets = tuple(secret for secret in secrets if secret)
        self.observer = observer
        self.sequence = 0

    def __enter__(self):
        self.file = self.path.open("x", encoding="utf-8")
        return self

    def __exit__(self, *exc):
        self.file.close()

    def _redact(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
        elif isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        elif isinstance(value, (list, tuple)):
            return [self._redact(item) for item in value]
        return value

    def write(self, event: str, **fields):
        self.sequence += 1
        record = self._redact({
            "sequence": self.sequence, "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event, **fields,
        })
        self.file.write(json.dumps(record, ensure_ascii=True) + "\n")
        self.file.flush()
        if self.observer is not None:
            self.observer(record)
