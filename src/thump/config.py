"""YAML is the single source of truth. Invalid config is fatal at boot."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from thump.models import Check, CheckType
from thump.schedule import CronSchedule, ScheduleError
from thump.tokens import derive_token

VALID_DRIVERS = ("sqlite", "redis")
_DURATION_RE = re.compile(r"(\d+)([smhd])")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


class ConfigError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("invalid config:\n  - " + "\n  - ".join(errors))


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses silently shadowed mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as e:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from e
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def parse_duration(s: str) -> timedelta:
    if not isinstance(s, str):
        raise ValueError(f"invalid duration: {s!r}")
    text = s.strip()
    parts = _DURATION_RE.findall(text)
    if not text or "".join(n + u for n, u in parts) != text:
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


def _mapping(value: object, where: str, errors: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{where} must be a mapping")
        return {}
    if not all(isinstance(key, str) for key in value):
        errors.append(f"{where} keys must be strings")
        return {}
    return cast(dict[str, Any], value)


def _unknown(raw: Mapping[str, object], allowed: set[str], where: str, errors: list[str]) -> None:
    for key in sorted(set(raw) - allowed):
        errors.append(f"{where}: unknown key {key!r}")


def _string(
    raw: Mapping[str, object],
    key: str,
    default: str | None,
    errors: list[str],
    where: str,
    *,
    minimum_length: int = 1,
) -> str | None:
    if key not in raw:
        return default
    value = raw[key]
    if type(value) is not str or len(value.strip()) < minimum_length:
        requirement = (
            "a nonempty string"
            if minimum_length == 1
            else f"a string of at least {minimum_length} characters"
        )
        errors.append(f"{where}: {key} must be {requirement}")
        return default
    return value


def _duration(
    raw: Mapping[str, object],
    key: str,
    default: timedelta,
    errors: list[str],
    where: str,
    *,
    allow_zero: bool = False,
) -> timedelta:
    if key not in raw:
        return default
    value = raw[key]
    if type(value) is not str:
        errors.append(f"{where}: {key} must be a duration string, got {value!r}")
        return default
    try:
        parsed = parse_duration(value)
    except ValueError as e:
        errors.append(f"{where}: {key}: {e}")
        return default
    if parsed < timedelta(0) or (not allow_zero and parsed == timedelta(0)):
        comparison = "non-negative" if allow_zero else "greater than zero"
        errors.append(f"{where}: {key} must be {comparison}")
        return default
    return parsed


def _int(
    raw: Mapping[str, object],
    key: str,
    default: int,
    errors: list[str],
    where: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if type(value) is not int:
        errors.append(f"{where}: {key} must be an integer, got {value!r}")
        return default
    if value < minimum or maximum is not None and value > maximum:
        bound = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        errors.append(f"{where}: {key} must be {bound}, got {value}")
        return default
    return value


def _bool(raw: Mapping[str, object], key: str, default: bool, errors: list[str], where: str) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if type(value) is not bool:
        errors.append(f"{where}: {key} must be a boolean, got {value!r}")
        return default
    return value


def _valid_listen(listen: str) -> bool:
    host, separator, port_text = listen.rpartition(":")
    if not separator or not port_text.isascii() or not port_text.isdecimal():
        return False
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return False
    port = int(port_text)
    return 1 <= port <= 65535


def parse_config(text: str, env: Mapping[str, str]) -> Config:
    errors: list[str] = []
    expanded = _expand_env(text, env, errors)
    if errors:
        raise ConfigError(errors)
    try:
        loaded: object = yaml.load(expanded, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as e:
        raise ConfigError([f"malformed YAML: {e}"]) from e

    doc = _mapping(loaded, "config", errors)
    _unknown(doc, {"store", "server", "defaults", "checks"}, "config", errors)

    raw_store = _mapping(doc.get("store"), "store", errors)
    _unknown(raw_store, {"driver", "dsn"}, "store", errors)
    driver = _string(raw_store, "driver", "sqlite", errors, "store") or "sqlite"
    if driver not in VALID_DRIVERS:
        errors.append(f"store.driver must be one of {VALID_DRIVERS}, got {driver!r}")
    dsn = _string(raw_store, "dsn", "./thump.db", errors, "store") or "./thump.db"
    store = StoreConfig(driver=driver, dsn=dsn)

    raw_server = _mapping(doc.get("server"), "server", errors)
    _unknown(raw_server, {"listen", "secret", "admin_token", "timezone"}, "server", errors)
    secret = _string(
        raw_server, "secret", None, errors, "server", minimum_length=16
    )
    if secret is None and "secret" not in raw_server:
        errors.append("server.secret is required")
    listen = _string(raw_server, "listen", ":8080", errors, "server") or ":8080"
    if not _valid_listen(listen):
        errors.append(f"server.listen must be HOST:PORT with a port from 1 to 65535, got {listen!r}")
    tz_name = _string(raw_server, "timezone", "UTC", errors, "server") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"server.timezone: unknown IANA timezone {tz_name!r}")
        tz = ZoneInfo("UTC")
    admin = _string(
        raw_server, "admin_token", None, errors, "server", minimum_length=16
    )
    server = ServerConfig(listen=listen, secret=secret or "", admin_token=admin, timezone=tz)

    defaults = _mapping(doc.get("defaults"), "defaults", errors)
    _unknown(defaults, {"grace", "timeout", "history", "failure_threshold"}, "defaults", errors)
    def_grace = _duration(
        defaults, "grace", timedelta(minutes=5), errors, "defaults", allow_zero=True
    )
    def_timeout = _duration(defaults, "timeout", timedelta(seconds=10), errors, "defaults")
    def_history = _int(defaults, "history", 100, errors, "defaults", minimum=1)
    def_threshold = _int(defaults, "failure_threshold", 2, errors, "defaults", minimum=1)

    raw_checks = doc.get("checks", [])
    if not isinstance(raw_checks, list):
        errors.append("checks must be a list")
        raw_checks = []

    checks: list[Check] = []
    names: set[str] = set()
    tokens: set[str] = set()
    allowed_check = {
        "name", "type", "interval", "schedule", "grace", "timeout", "history",
        "failure_threshold", "token", "enabled", "url", "expect_status",
    }
    for i, value in enumerate(raw_checks):
        item_where = f"checks[{i}]"
        raw = _mapping(value, item_where, errors)
        if not raw:
            continue
        _unknown(raw, allowed_check, item_where, errors)
        name = _string(raw, "name", None, errors, item_where)
        if name is None:
            if "name" not in raw:
                errors.append(f"{item_where}: name is required")
            continue
        where = f"check {name!r}"
        if name in names:
            errors.append(f"{where}: duplicate check name")
            continue
        names.add(name)

        type_raw = _string(raw, "type", None, errors, where)
        if type_raw not in ("heartbeat", "probe"):
            errors.append(f"{where}: type must be 'heartbeat' or 'probe', got {type_raw!r}")
            continue
        ctype = CheckType(type_raw)

        url = _string(raw, "url", None, errors, where)
        if ctype is CheckType.PROBE:
            if url is None:
                errors.append(f"{where}: probe checks require a url")
            elif urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc:
                errors.append(f"{where}: url must be an absolute http or https URL")
        elif "url" in raw:
            errors.append(f"{where}: heartbeat checks must not have a url")

        has_interval = "interval" in raw
        has_schedule = "schedule" in raw
        schedule: CronSchedule | None = None
        if ctype is CheckType.PROBE:
            if has_schedule:
                errors.append(f"{where}: schedule is only valid for heartbeat checks")
            if not has_interval:
                errors.append(f"{where}: interval is required")
        else:
            if has_interval and has_schedule:
                errors.append(f"{where}: interval and schedule are mutually exclusive")
            elif not has_interval and not has_schedule:
                errors.append(f"{where}: heartbeat requires either interval or schedule")
            elif has_schedule:
                schedule_text = _string(raw, "schedule", None, errors, where)
                if schedule_text is not None:
                    try:
                        schedule = CronSchedule.parse(schedule_text, server.timezone)
                    except ScheduleError as e:
                        errors.append(f"{where}: {e}")

        interval = _duration(raw, "interval", timedelta(hours=1), errors, where) if has_interval else None
        token = _string(
            raw, "token", None, errors, where, minimum_length=16
        ) or derive_token(server.secret, name)
        if token in tokens:
            errors.append(f"{where}: duplicate effective token")
        tokens.add(token)
        checks.append(
            Check(
                name=name,
                type=ctype,
                interval=interval,
                grace=_duration(raw, "grace", def_grace, errors, where, allow_zero=True),
                timeout=_duration(raw, "timeout", def_timeout, errors, where),
                history=_int(raw, "history", def_history, errors, where, minimum=1),
                failure_threshold=_int(
                    raw, "failure_threshold", def_threshold, errors, where, minimum=1
                ),
                token=token,
                enabled=_bool(raw, "enabled", True, errors, where),
                url=url,
                expect_status=_int(
                    raw, "expect_status", 200, errors, where, minimum=100, maximum=599
                ),
                schedule=schedule,
            )
        )

    if errors:
        raise ConfigError(errors)
    return Config(store=store, server=server, checks=tuple(checks))


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> Config:
    import os

    return parse_config(Path(path).read_text(), env if env is not None else os.environ)
