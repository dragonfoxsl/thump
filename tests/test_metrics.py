from datetime import datetime, timedelta, timezone

from thump.config import parse_config
from thump.metrics import render_metrics
from thump.models import CheckState

YAML = """
store: {driver: sqlite, dsn: ':memory:'}
server: {listen: ":8080", secret: ${THUMP_SECRET}}
checks:
  - name: nightly-db-backup
    type: heartbeat
    interval: 24h
    grace: 1h
"""
ENV = {"THUMP_SECRET": "s3cret"}
T0 = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


def test_up_check_renders_1():
    cfg = parse_config(YAML, ENV)
    states = {"nightly-db-backup": CheckState(last_seen=T0, last_result_ok=True)}
    out = render_metrics(cfg, states, T0, unknown_pings=0)
    assert 'thump_check_up{name="nightly-db-backup"} 1' in out


def test_down_check_renders_0():
    cfg = parse_config(YAML, ENV)
    states = {
        "nightly-db-backup": CheckState(
            last_seen=T0 - timedelta(hours=26), last_result_ok=True
        )
    }
    out = render_metrics(cfg, states, T0, unknown_pings=0)
    assert 'thump_check_up{name="nightly-db-backup"} 0' in out


def test_last_seen_is_a_unix_timestamp():
    cfg = parse_config(YAML, ENV)
    states = {"nightly-db-backup": CheckState(last_seen=T0, last_result_ok=True)}
    out = render_metrics(cfg, states, T0, unknown_pings=0)
    assert f'thump_check_last_seen_seconds{{name="nightly-db-backup"}} {T0.timestamp()}' in out


def test_unknown_ping_counter_is_exposed():
    cfg = parse_config(YAML, ENV)
    out = render_metrics(cfg, {"nightly-db-backup": CheckState()}, T0, unknown_pings=3)
    assert "thump_unknown_ping_total 3" in out


def test_help_and_type_lines_are_present():
    cfg = parse_config(YAML, ENV)
    out = render_metrics(cfg, {"nightly-db-backup": CheckState()}, T0, unknown_pings=0)
    assert "# HELP thump_check_up" in out
    assert "# TYPE thump_check_up gauge" in out


def test_consecutive_failures_gauge_is_exposed():
    cfg = parse_config(YAML, ENV)
    states = {
        "nightly-db-backup": CheckState(last_result_ok=False, consecutive_failures=3)
    }
    out = render_metrics(cfg, states, T0, unknown_pings=0)
    assert 'thump_check_consecutive_failures{name="nightly-db-backup"} 3' in out


def test_build_info_carries_the_version():
    cfg = parse_config(YAML, ENV)
    out = render_metrics(
        cfg, {"nightly-db-backup": CheckState()}, T0, unknown_pings=0, version="1.2.3"
    )
    assert 'thump_build_info{version="1.2.3"} 1' in out
