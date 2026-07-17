"""YAML is the single source of truth. Invalid config is fatal at boot.

A monitor that starts half-configured and silently fails to watch something is
worse than one that refuses to start: the first failure mode is invisible, the
second is a CrashLoopBackOff noticed in thirty seconds.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from tskmon.models import Check, CheckType
from tskmon.tokens import derive_token

VALID_DRIVERS = ("sqlite", "redis")

_DURATION_RE = re.compile(r"(\d+)([smhd])")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


class ConfigError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("invalid config:\n  - " + "\n  - ".join(errors))


def parse_duration(s: str) -> timedelta:
    text = str(s).strip()
    if not text or not _DURATION_RE.fullmatch(text) and not _DURATION_RE.match(text):
        raise ValueError(f"invalid duration: {s!r}")
    parts = _DURATION_RE.findall(text)
    if "".join(n + u for n, u in parts) != text:
        raise ValueError(f"invalid duration: {s!r}")
    total = timedelta()
    for number, unit in parts:
        total += timedelta(**{_UNITS[unit]: int(number)})
    return total


@dataclass(frozen=True, slots=True)
class StoreConfig:
    driver: str
    dsn: str


@dataclass(frozen=True, slots=True)
class ServerConfig:
    listen: str
    secret: str
    admin_token: str | None
    timezone: ZoneInfo


@dataclass(frozen=True, slots=True)
class Config:
    store: StoreConfig
    server: ServerConfig
    checks: tuple[Check, ...]

    @property
    def by_name(self) -> dict[str, Check]:
        return {c.name: c for c in self.checks}

    @property
    def by_token(self) -> dict[str, Check]:
        return {c.token: c for c in self.checks}


def _expand_env(text: str, env: Mapping[str, str], errors: list[str]) -> str:
    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in env:
            errors.append(f"environment variable {name} is not set")
            return ""
        return env[name]

    return _ENV_RE.sub(sub, text)


def _duration(raw: dict[str, Any], key: str, default: timedelta, errors: list[str], where: str) -> timedelta:
    if key not in raw:
        return default
    try:
        return parse_duration(raw[key])
    except ValueError as e:
        errors.append(f"{where}: {e}")
        return default


def _int(raw: dict[str, Any], key: str, default: int, errors: list[str], where: str) -> int:
    if key not in raw:
        return default
    value = raw[key]
    # bool is a subclass of int; `history: true` must not silently become 1.
    if isinstance(value, bool):
        errors.append(f"{where}: {key} must be an integer, got {value!r}")
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        errors.append(f"{where}: {key} must be an integer, got {value!r}")
        return default


def parse_config(text: str, env: Mapping[str, str]) -> Config:
    errors: list[str] = []
    expanded = _expand_env(text, env, errors)
    if errors:
        raise ConfigError(errors)

    doc = yaml.safe_load(expanded) or {}

    raw_store = doc.get("store") or {}
    driver = str(raw_store.get("driver", "sqlite"))
    if driver not in VALID_DRIVERS:
        errors.append(f"store.driver must be one of {VALID_DRIVERS}, got {driver!r}")
    store = StoreConfig(driver=driver, dsn=str(raw_store.get("dsn", "./tskmon.db")))

    raw_server = doc.get("server") or {}
    secret = raw_server.get("secret")
    if not secret:
        errors.append("server.secret is required")
    tz_name = str(raw_server.get("timezone", "UTC"))
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"server.timezone: unknown IANA timezone {tz_name!r}")
        tz = ZoneInfo("UTC")
    admin = raw_server.get("admin_token") or None
    server = ServerConfig(
        listen=str(raw_server.get("listen", ":8080")),
        secret=str(secret or ""),
        admin_token=str(admin) if admin else None,
        timezone=tz,
    )

    d = doc.get("defaults") or {}
    def_grace = _duration(d, "grace", timedelta(minutes=5), errors, "defaults.grace")
    def_timeout = _duration(d, "timeout", timedelta(seconds=10), errors, "defaults.timeout")
    def_history = _int(d, "history", 100, errors, "defaults.history")
    def_threshold = _int(d, "failure_threshold", 2, errors, "defaults.failure_threshold")

    checks: list[Check] = []
    seen: set[str] = set()
    for i, raw in enumerate(doc.get("checks") or []):
        where = f"checks[{i}]"
        name = raw.get("name")
        if not name:
            errors.append(f"{where}: name is required")
            continue
        where = f"check {name!r}"
        if name in seen:
            errors.append(f"{where}: duplicate check name")
            continue
        seen.add(name)

        type_raw = str(raw.get("type", ""))
        if type_raw not in ("heartbeat", "probe"):
            errors.append(f"{where}: type must be 'heartbeat' or 'probe', got {type_raw!r}")
            continue
        ctype = CheckType(type_raw)

        url = raw.get("url")
        if ctype is CheckType.PROBE and not url:
            errors.append(f"{where}: probe checks require a url")
        if ctype is CheckType.HEARTBEAT and url:
            errors.append(f"{where}: heartbeat checks must not have a url")

        if "interval" not in raw:
            errors.append(f"{where}: interval is required")
            continue
        interval = _duration(raw, "interval", timedelta(hours=1), errors, where)

        checks.append(
            Check(
                name=str(name),
                type=ctype,
                interval=interval,
                grace=_duration(raw, "grace", def_grace, errors, where),
                timeout=_duration(raw, "timeout", def_timeout, errors, where),
                history=_int(raw, "history", def_history, errors, where),
                failure_threshold=_int(raw, "failure_threshold", def_threshold, errors, where),
                token=str(raw.get("token") or derive_token(server.secret, str(name))),
                enabled=bool(raw.get("enabled", True)),
                url=str(url) if url else None,
                expect_status=_int(raw, "expect_status", 200, errors, where),
            )
        )

    if errors:
        raise ConfigError(errors)
    return Config(store=store, server=server, checks=tuple(checks))


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> Config:
    import os

    return parse_config(Path(path).read_text(), env if env is not None else os.environ)
