import json
import logging

from thump.logconfig import JsonFormatter, build_formatter


def _record(msg: str, *args: object, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("thump", level, "f.py", 10, msg, args, None)


def test_json_formatter_emits_structured_fields():
    out = JsonFormatter().format(_record("probe %s down", "api", level=logging.WARNING))
    d = json.loads(out)
    assert d["level"] == "WARNING"
    assert d["logger"] == "thump"
    assert d["message"] == "probe api down"
    assert "time" in d


def test_json_formatter_includes_exception_text():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord(
            "thump", logging.ERROR, "f.py", 10, "failed", (), sys.exc_info()
        )
    d = json.loads(JsonFormatter().format(rec))
    assert "ValueError: boom" in d["exception"]


def test_build_formatter_selects_json():
    assert isinstance(build_formatter("json"), JsonFormatter)


def test_build_formatter_defaults_to_plain_text():
    assert not isinstance(build_formatter("text"), JsonFormatter)
