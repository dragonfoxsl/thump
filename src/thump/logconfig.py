"""Log formatting. Plain text by default; set THUMP_LOG_FORMAT=json for
one-line-per-record JSON that Loki/ELK/Datadog can ingest without a regex.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def build_formatter(fmt: str) -> logging.Formatter:
    if fmt.lower() == "json":
        return JsonFormatter()
    return logging.Formatter(_TEXT_FORMAT)
