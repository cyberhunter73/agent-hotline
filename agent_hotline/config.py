"""Config loading and validation for agent-hotline.

Fail-closed by design: anything that controls who can be called, or how often,
defaults to the restrictive value when absent. Required values raise ConfigError.

M1 fix-wave sweep: every validation below now happens at config LOAD time,
not at first use. Before this sweep, several of these were deferred bugs
waiting to happen: a malformed `inbound.allowed_numbers` entry was only
caught when `InboundGate` was first constructed in `entrypoint` (not at
`load_config()`); `silence_timeout_s`/`max_call_length_s`/
`max_calls_per_24h` were parsed with a bare `int(...)` that raised an
uncaught `ValueError`, not `ConfigError`, on bad input, and accepted zero or
negative values; `outbound.destination_number` was never checked at all,
even when `outbound.enabled` is true; `quiet_hours` `HH:MM` strings and
`quiet_hours.timezone` were never validated until the first `notify()` call
(a raw `ValueError`/`zoneinfo.ZoneInfoNotFoundError`, not `ConfigError`).
I5 fix: `telephony_provider`/`agent_backend`/`stt.engine`/`tts.engine` are now
checked against the one implementation this codebase actually has for each,
instead of being parsed as unchecked strings and silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

# I5: each of these is a small, explicit allowed set that can grow when a
# second real implementation lands later — this is a validation tightening,
# not a config format change.
ALLOWED_TELEPHONY_PROVIDERS = {"telnyx"}
ALLOWED_AGENT_BACKENDS = {"claude-agent-sdk"}
ALLOWED_STT_ENGINES = {"faster-whisper"}
ALLOWED_TTS_ENGINES = {"kokoro"}


class ConfigError(Exception):
    """Raised when config is missing a required value or contains an invalid one."""


@dataclass
class SttConfig:
    engine: str
    # None only when the whole `stt` section is absent (see _parse_stt). When the
    # section is present, device is always an explicit value that is not "auto".
    device: str | None


@dataclass
class TtsConfig:
    engine: str
    voice: str


@dataclass
class InboundConfig:
    allowed_numbers: list[str]
    quit_phrases: list[str]
    silence_timeout_s: int
    max_call_length_s: int


@dataclass
class QuietHours:
    start: str
    end: str
    timezone: str


@dataclass
class EventTypeConfig:
    urgent: bool


@dataclass
class OutboundConfig:
    enabled: bool
    destination_number: str
    max_calls_per_24h: int
    quiet_hours: QuietHours
    event_types: dict[str, EventTypeConfig] = field(default_factory=dict)
    # C2: where the persistent (sqlite) outbound call-history store lives,
    # so max_calls_per_24h holds across separate CLI invocations.
    history_path: str = "~/.agent-hotline/outbound_calls.sqlite3"


@dataclass
class Config:
    telephony_provider: str
    agent_backend: str
    stt: SttConfig
    tts: TtsConfig
    inbound: InboundConfig
    outbound: OutboundConfig


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{key}' must be a mapping")
    return value


def _validate_choice(value: str, allowed: set[str], field_name: str) -> str:
    if value not in allowed:
        raise ConfigError(
            f"only {sorted(allowed)!r} implemented for {field_name}, got {value!r}"
        )
    return value


def _parse_positive_int(section: dict[str, Any], key: str, default: int, field_name: str) -> int:
    raw = section.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{field_name} must be a positive integer, got {value}")
    return value


def _validate_hhmm(value: str, field_name: str) -> None:
    parts = value.split(":")
    if len(parts) != 2:
        raise ConfigError(f"{field_name} must be in HH:MM form, got {value!r}")
    hour_str, minute_str = parts
    try:
        hour = int(hour_str)
        minute = int(minute_str)
    except ValueError as exc:
        raise ConfigError(f"{field_name} must be in HH:MM form, got {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"{field_name} must be a valid HH:MM time, got {value!r}")


def _validate_timezone(tz_name: str, field_name: str) -> None:
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{field_name} is not a recognized IANA timezone: {tz_name!r}") from exc


def _validate_phone_number(raw: str, field_name: str) -> None:
    # Imported here (not at module level) to avoid a circular import:
    # inbound.py has no reason to import config.py, so this is one-directional,
    # but keeping the import local to this function keeps that direction obvious.
    from agent_hotline.inbound import normalize_number

    try:
        normalize_number(raw)
    except ValueError as exc:
        raise ConfigError(f"{field_name} is not a valid phone number: {raw!r} ({exc})") from exc


def _parse_stt(data: dict[str, Any]) -> SttConfig:
    section = _section(data, "stt")
    engine = str(section.get("engine", "faster-whisper"))
    _validate_choice(engine, ALLOWED_STT_ENGINES, "stt.engine")
    if not section:
        # No stt section at all: no device is chosen for the caller. Consumers
        # must check device before constructing the STT engine.
        return SttConfig(engine=engine, device=None)
    if "device" not in section:
        raise ConfigError("stt.device must be set explicitly (e.g. 'cpu' or 'cuda')")
    device = section["device"]
    if not isinstance(device, str) or not device.strip():
        raise ConfigError("stt.device must be a non-empty string")
    if device.strip().lower() == "auto":
        raise ConfigError("stt.device may not be 'auto'; set it explicitly (e.g. 'cpu')")
    return SttConfig(engine=engine, device=device.strip())


def _parse_inbound(data: dict[str, Any]) -> InboundConfig:
    section = _section(data, "inbound")
    if "allowed_numbers" not in section:
        raise ConfigError("inbound.allowed_numbers is required")
    allowed = section["allowed_numbers"]
    if not isinstance(allowed, list) or not all(isinstance(n, str) for n in allowed):
        raise ConfigError("inbound.allowed_numbers must be a list of strings")
    # M1: validate every entry through normalize_number at LOAD time, not
    # lazily whenever InboundGate first happens to be constructed — a typo
    # in config must fail loudly and immediately, not silently drop a
    # caller the first time the worker actually starts.
    for entry in allowed:
        _validate_phone_number(entry, "inbound.allowed_numbers entry")

    quit_phrases = section.get("quit_phrases", ["goodbye"])
    if not isinstance(quit_phrases, list) or not all(isinstance(p, str) for p in quit_phrases):
        raise ConfigError("inbound.quit_phrases must be a list of strings")

    return InboundConfig(
        allowed_numbers=list(allowed),
        quit_phrases=list(quit_phrases),
        silence_timeout_s=_parse_positive_int(section, "silence_timeout_s", 30, "inbound.silence_timeout_s"),
        max_call_length_s=_parse_positive_int(section, "max_call_length_s", 1800, "inbound.max_call_length_s"),
    )


def _parse_outbound(data: dict[str, Any]) -> OutboundConfig:
    section = _section(data, "outbound")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        # A string like "false" would be truthy; refuse rather than guess.
        raise ConfigError("outbound.enabled must be true or false")

    destination_number = str(section.get("destination_number", ""))
    if enabled:
        # M1: required and validated, but only when outbound is actually
        # turned on — an unused placeholder value in a disabled config
        # should not block loading.
        if not destination_number:
            raise ConfigError("outbound.destination_number is required when outbound.enabled is true")
        _validate_phone_number(destination_number, "outbound.destination_number")

    quiet = _section(section, "quiet_hours")
    quiet_start = str(quiet.get("start", "22:00"))
    quiet_end = str(quiet.get("end", "07:00"))
    quiet_timezone = str(quiet.get("timezone", "UTC"))
    _validate_hhmm(quiet_start, "outbound.quiet_hours.start")
    _validate_hhmm(quiet_end, "outbound.quiet_hours.end")
    # I4: validated at load time so an unrecognized zone name fails loudly
    # here, not with an uncaught zoneinfo error buried inside the first
    # notify() call.
    _validate_timezone(quiet_timezone, "outbound.quiet_hours.timezone")
    quiet_hours = QuietHours(start=quiet_start, end=quiet_end, timezone=quiet_timezone)

    event_types: dict[str, EventTypeConfig] = {}
    raw_events = section.get("event_types") or {}
    if not isinstance(raw_events, dict):
        raise ConfigError("outbound.event_types must be a mapping")
    for name, body in raw_events.items():
        body = body or {}
        if not isinstance(body, dict):
            raise ConfigError(f"outbound.event_types.{name} must be a mapping")
        urgent = body.get("urgent", False)
        if not isinstance(urgent, bool):
            raise ConfigError(f"outbound.event_types.{name}.urgent must be true or false")
        event_types[str(name)] = EventTypeConfig(urgent=urgent)

    return OutboundConfig(
        enabled=enabled,
        destination_number=destination_number,
        max_calls_per_24h=_parse_positive_int(section, "max_calls_per_24h", 1, "outbound.max_calls_per_24h"),
        quiet_hours=quiet_hours,
        event_types=event_types,
        history_path=str(section.get("history_path", "~/.agent-hotline/outbound_calls.sqlite3")),
    )


def load_config_from_dict(data: dict[str, Any]) -> Config:
    """Validate a parsed config mapping and return a Config."""
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping")

    telephony_provider = str(data.get("telephony_provider", "telnyx"))
    _validate_choice(telephony_provider, ALLOWED_TELEPHONY_PROVIDERS, "telephony_provider")
    agent_backend = str(data.get("agent_backend", "claude-agent-sdk"))
    _validate_choice(agent_backend, ALLOWED_AGENT_BACKENDS, "agent_backend")

    tts = _section(data, "tts")
    tts_engine = str(tts.get("engine", "kokoro"))
    _validate_choice(tts_engine, ALLOWED_TTS_ENGINES, "tts.engine")

    return Config(
        telephony_provider=telephony_provider,
        agent_backend=agent_backend,
        stt=_parse_stt(data),
        tts=TtsConfig(
            engine=tts_engine,
            voice=str(tts.get("voice", "af_heart")),
        ),
        inbound=_parse_inbound(data),
        outbound=_parse_outbound(data),
    )


def load_config(path: str) -> Config:
    """Read a YAML config file from `path` and return a validated Config."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        raise ConfigError(f"config file is empty: {path}")
    return load_config_from_dict(data)
