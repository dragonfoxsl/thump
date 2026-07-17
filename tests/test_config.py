from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from tskmon.config import ConfigError, parse_config, parse_duration
from tskmon.models import CheckType
from tskmon.tokens import derive_token

MINIMAL = """
store:
  driver: sqlite
  dsn: /var/lib/tskmon/state.db
server:
  listen: ":8080"
  secret: ${TSKMON_SECRET}
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
"""

ENV = {"TSKMON_SECRET": "s3cret", "TSKMON_ADMIN_TOKEN": "admin-tok"}


def test_parse_duration_units():
    assert parse_duration("30s") == timedelta(seconds=30)
    assert parse_duration("5m") == timedelta(minutes=5)
    assert parse_duration("24h") == timedelta(hours=24)
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("1h30m") == timedelta(hours=1, minutes=30)


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_env_expansion_keeps_secret_out_of_the_file():
    cfg = parse_config(MINIMAL, ENV)
    assert cfg.server.secret == "s3cret"


def test_missing_env_var_is_fatal():
    with pytest.raises(ConfigError):
        parse_config(MINIMAL, {})


def test_defaults_are_applied():
    cfg = parse_config(MINIMAL, ENV)
    c = cfg.by_name["nightly-db-backup"]
    assert c.grace == timedelta(minutes=5)
    assert c.timeout == timedelta(seconds=10)
    assert c.history == 100
    assert c.failure_threshold == 2
    assert c.enabled is True


def test_per_check_override_beats_default():
    text = MINIMAL + "    grace: 1h\n    history: 20\n"
    c = parse_config(text, ENV).by_name["nightly-db-backup"]
    assert c.grace == timedelta(hours=1)
    assert c.history == 20


def test_token_is_derived_when_not_declared():
    cfg = parse_config(MINIMAL, ENV)
    c = cfg.by_name["nightly-db-backup"]
    assert c.token == derive_token("s3cret", "nightly-db-backup")
    assert cfg.by_token[c.token] is c


def test_explicit_token_overrides_derived():
    text = MINIMAL + "    token: 7c9f2a\n"
    cfg = parse_config(text, ENV)
    assert cfg.by_name["nightly-db-backup"].token == "7c9f2a"
    assert "7c9f2a" in cfg.by_token


def test_timezone_defaults_to_utc():
    assert parse_config(MINIMAL, ENV).server.timezone == ZoneInfo("UTC")


def test_timezone_is_parsed():
    text = MINIMAL.replace(
        'secret: ${TSKMON_SECRET}',
        'secret: ${TSKMON_SECRET}\n  timezone: Asia/Kolkata',
    )
    assert parse_config(text, ENV).server.timezone == ZoneInfo("Asia/Kolkata")


def test_unknown_timezone_is_fatal():
    text = MINIMAL.replace(
        'secret: ${TSKMON_SECRET}',
        'secret: ${TSKMON_SECRET}\n  timezone: Mars/Olympus',
    )
    with pytest.raises(ConfigError):
        parse_config(text, ENV)


def test_admin_token_is_optional_and_none_when_absent():
    assert parse_config(MINIMAL, ENV).server.admin_token is None


def test_probe_check_is_parsed():
    text = """
store: {driver: sqlite, dsn: ./s.db}
server: {listen: ":8080", secret: ${TSKMON_SECRET}}
checks:
  - name: internal-payments-api
    type: probe
    url: http://payments.internal:8080/healthz
    interval: 60s
    expect_status: 204
"""
    c = parse_config(text, ENV).by_name["internal-payments-api"]
    assert c.type is CheckType.PROBE
    assert c.url == "http://payments.internal:8080/healthz"
    assert c.expect_status == 204


# --- validation: a half-configured monitor is worse than one that won't start ---

def test_duplicate_check_names_are_fatal():
    text = MINIMAL + "  - name: nightly-db-backup\n    type: heartbeat\n    interval: 1h\n"
    with pytest.raises(ConfigError):
        parse_config(text, ENV)


def test_probe_without_url_is_fatal():
    text = """
store: {driver: sqlite, dsn: ./s.db}
server: {listen: ":8080", secret: ${TSKMON_SECRET}}
checks:
  - name: p
    type: probe
    interval: 60s
"""
    with pytest.raises(ConfigError):
        parse_config(text, ENV)


def test_heartbeat_with_url_is_fatal():
    text = MINIMAL + "    url: http://nope\n"
    with pytest.raises(ConfigError):
        parse_config(text, ENV)


def test_unknown_store_driver_is_fatal():
    with pytest.raises(ConfigError):
        parse_config(MINIMAL.replace("driver: sqlite", "driver: mongodb"), ENV)


def test_all_errors_are_reported_together():
    text = """
store: {driver: mongodb, dsn: ./s.db}
server: {listen: ":8080", secret: ${TSKMON_SECRET}}
checks:
  - name: p
    type: probe
    interval: 60s
"""
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert len(exc.value.errors) == 2  # bad driver AND probe-without-url


def test_malformed_integer_field_is_fatal_not_a_crash():
    text = MINIMAL + "    history: many\n"
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert any("history" in e for e in exc.value.errors)


def test_malformed_integer_is_collected_with_other_errors():
    # A bad driver AND a bad integer must report together — not crash on the first.
    text = MINIMAL.replace("driver: sqlite", "driver: mongodb") + "    history: lots\n"
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert len(exc.value.errors) == 2


def test_boolean_is_rejected_as_integer():
    # YAML `true` becomes Python True (an int subclass); silently accepting it
    # as history=1 would be a latent bug.
    text = MINIMAL + "    failure_threshold: true\n"
    with pytest.raises(ConfigError):
        parse_config(text, ENV)
