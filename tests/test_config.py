from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from thump.config import ConfigError, parse_config, parse_duration
from thump.models import CheckType
from thump.tokens import derive_token

MINIMAL = """
store:
  driver: sqlite
  dsn: /var/lib/thump/state.db
server:
  listen: ":8080"
  secret: ${THUMP_SECRET}
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
"""

ENV = {"THUMP_SECRET": "test-secret-at-least-16-chars", "THUMP_ADMIN_TOKEN": "admin-token-at-least-16-chars"}


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
    assert cfg.server.secret == "test-secret-at-least-16-chars"


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
    assert c.token == derive_token("test-secret-at-least-16-chars", "nightly-db-backup")
    assert cfg.by_token[c.token] is c


def test_explicit_token_overrides_derived():
    text = MINIMAL + "    token: explicit-token-long-enough\n"
    cfg = parse_config(text, ENV)
    assert cfg.by_name["nightly-db-backup"].token == "explicit-token-long-enough"
    assert "explicit-token-long-enough" in cfg.by_token


def test_timezone_defaults_to_utc():
    assert parse_config(MINIMAL, ENV).server.timezone == ZoneInfo("UTC")


def test_timezone_is_parsed():
    text = MINIMAL.replace(
        'secret: ${THUMP_SECRET}',
        'secret: ${THUMP_SECRET}\n  timezone: Asia/Kolkata',
    )
    assert parse_config(text, ENV).server.timezone == ZoneInfo("Asia/Kolkata")


def test_unknown_timezone_is_fatal():
    text = MINIMAL.replace(
        'secret: ${THUMP_SECRET}',
        'secret: ${THUMP_SECRET}\n  timezone: Mars/Olympus',
    )
    with pytest.raises(ConfigError):
        parse_config(text, ENV)


def test_admin_token_is_optional_and_none_when_absent():
    assert parse_config(MINIMAL, ENV).server.admin_token is None


def test_probe_check_is_parsed():
    text = """
store: {driver: sqlite, dsn: ./s.db}
server: {listen: ":8080", secret: ${THUMP_SECRET}}
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
server: {listen: ":8080", secret: ${THUMP_SECRET}}
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
server: {listen: ":8080", secret: ${THUMP_SECRET}}
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


@pytest.mark.parametrize(
    "text",
    [
        "server: [not, a, mapping]",
        "checks: {not: a-list}",
        "- not-a-mapping",
        "server: [",
    ],
)
def test_malformed_yaml_and_shapes_raise_clean_config_error(text):
    with pytest.raises(ConfigError):
        parse_config(text, ENV)


def test_duplicate_yaml_keys_are_rejected():
    text = MINIMAL.replace(
        '  listen: ":8080"', '  listen: ":8080"\n  listen: ":9090"'
    )
    with pytest.raises(ConfigError, match="duplicate key"):
        parse_config(text, ENV)


@pytest.mark.parametrize(
    ("suffix", "field"),
    [
        ("    enabled: 'false'\n", "enabled"),
        ("    history: '3'\n", "history"),
        ("    interval: 0s\n", "interval"),
        ("    timeout: 0s\n", "timeout"),
        ("    grace: -1s\n", "grace"),
        ("    history: 0\n", "history"),
        ("    failure_threshold: 0\n", "failure_threshold"),
        ("    expect_status: 99\n", "expect_status"),
        ("    surprise: true\n", "surprise"),
    ],
)
def test_invalid_exact_types_ranges_and_unknown_keys_are_rejected(suffix, field):
    with pytest.raises(ConfigError) as exc:
        parse_config(MINIMAL + suffix, ENV)
    assert field in str(exc.value)


def test_probe_url_must_be_http_or_https():
    text = """
server: {secret: ${THUMP_SECRET}}
checks:
  - {name: p, type: probe, url: 'ftp://example.com/a', interval: 1m}
"""
    with pytest.raises(ConfigError, match="http"):
        parse_config(text, ENV)


def test_effective_tokens_must_be_unique():
    token = "duplicate-token-long-enough"
    text = MINIMAL + f"  - {{name: other, type: heartbeat, interval: 1h, token: {token}}}\n"
    text = text.replace("    interval: 24h\n", f"    interval: 24h\n    token: {token}\n")
    with pytest.raises(ConfigError, match="token"):
        parse_config(text, ENV)


@pytest.mark.parametrize(
    "field",
    [
        "server.secret",
        "server.admin_token",
        "check.token",
    ],
)
def test_security_credentials_have_a_minimum_length(field):
    if field == "server.secret":
        text = MINIMAL
        env = {**ENV, "THUMP_SECRET": "short"}
    elif field == "server.admin_token":
        text = MINIMAL.replace(
            "  secret: ${THUMP_SECRET}\n",
            "  secret: ${THUMP_SECRET}\n  admin_token: short\n",
        )
        env = ENV
    else:
        text = MINIMAL + "    token: short\n"
        env = ENV
    with pytest.raises(ConfigError, match="at least 16"):
        parse_config(text, env)


@pytest.mark.parametrize("listen", ["localhost", ":0", ":70000", "1234", "host:not-a-port"])
def test_invalid_listen_address_is_a_config_error(listen):
    text = MINIMAL.replace('listen: ":8080"', f'listen: "{listen}"')
    with pytest.raises(ConfigError, match="listen"):
        parse_config(text, ENV)


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(ConfigError, match="unknown"):
        parse_config(MINIMAL + "surprise: true\n", ENV)


CRON = """
store:
  driver: sqlite
  dsn: /var/lib/thump/state.db
server:
  listen: ":8080"
  secret: ${THUMP_SECRET}
  timezone: America/New_York
checks:
  - name: nightly-db-backup
    type: heartbeat
    schedule: "0 2 * * *"
    grace: 30m
"""


def test_schedule_is_parsed_against_the_server_timezone():
    cfg = parse_config(CRON, ENV)
    check = cfg.checks[0]
    assert check.schedule is not None
    assert check.schedule.expr == "0 2 * * *"
    assert check.schedule.tz == ZoneInfo("America/New_York")
    assert check.interval is None


def test_heartbeat_with_neither_interval_nor_schedule_is_fatal():
    text = CRON.replace('    schedule: "0 2 * * *"\n', "")
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert any("interval or schedule" in e for e in exc.value.errors)


def test_heartbeat_with_both_interval_and_schedule_is_fatal():
    text = CRON.replace(
        '    schedule: "0 2 * * *"\n',
        '    schedule: "0 2 * * *"\n    interval: 24h\n',
    )
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert any("mutually exclusive" in e for e in exc.value.errors)


def test_probe_may_not_carry_a_schedule():
    text = """
store:
  driver: sqlite
server:
  secret: ${THUMP_SECRET}
checks:
  - name: payments
    type: probe
    url: http://payments.internal/healthz
    interval: 60s
    schedule: "0 2 * * *"
"""
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert any("only valid for heartbeat" in e for e in exc.value.errors)


def test_malformed_cron_expression_is_collected_as_a_config_error():
    text = CRON.replace('"0 2 * * *"', '"not a cron"')
    with pytest.raises(ConfigError) as exc:
        parse_config(text, ENV)
    assert any("cron expression" in e for e in exc.value.errors)


def test_interval_based_heartbeats_still_have_no_schedule():
    # Backward compatibility: the existing MINIMAL config is untouched.
    cfg = parse_config(MINIMAL, ENV)
    check = cfg.checks[0]
    assert check.schedule is None
    assert check.interval == timedelta(hours=24)
