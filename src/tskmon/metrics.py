"""Prometheus text rendering. Pure, like the evaluator: no I/O, `now` injected.

This endpoint is why the same box that adapts a private network to an EXTERNAL
uptime vendor also plugs straight into an INTERNAL Prometheus.
"""

from __future__ import annotations

from datetime import datetime

from tskmon.config import Config
from tskmon.evaluator import evaluate
from tskmon.models import CheckState, State


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics(
    config: Config,
    states: dict[str, CheckState],
    now: datetime,
    unknown_pings: int,
) -> str:
    lines = [
        "# HELP tskmon_check_up Whether the check is not DOWN (1) or DOWN (0).",
        "# TYPE tskmon_check_up gauge",
    ]
    for check in config.checks:
        state = evaluate(check, states.get(check.name, CheckState()), now)
        up = 0 if state is State.DOWN else 1
        lines.append(f'tskmon_check_up{{name="{_escape(check.name)}"}} {up}')

    lines += [
        "# HELP tskmon_check_last_seen_seconds Unix time of the last successful sighting.",
        "# TYPE tskmon_check_last_seen_seconds gauge",
    ]
    for check in config.checks:
        last_seen = states.get(check.name, CheckState()).last_seen
        if last_seen is not None:
            lines.append(
                f'tskmon_check_last_seen_seconds{{name="{_escape(check.name)}"}} {last_seen.timestamp()}'
            )

    lines += [
        "# HELP tskmon_unknown_ping_total Pings for checks that do not exist.",
        "# TYPE tskmon_unknown_ping_total counter",
        f"tskmon_unknown_ping_total {unknown_pings}",
        "",
    ]
    return "\n".join(lines)
