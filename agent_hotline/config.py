"""Config loading and validation for agent-hotline.

Fail-closed by design: anything that controls who can be called, or how often,
defaults to the restrictive value when absent. Required values raise ConfigError.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


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


def _parse_stt(data: dict[str, Any]) -> SttConfig:
    section = _section(data, "stt")
    engine = str(section.get("engine", "faster-whisper"))
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
    quit_phrases = section.get("quit_phrases", ["goodbye"])
    if not isinstance(quit_phrases, list) or not all(isinstance(p, str) for p in quit_phrases):
        raise ConfigError("inbound.quit_phrases must be a list of strings")
    return InboundConfig(
        allowed_numbers=list(allowed),
        quit_phrases=list(quit_phrases),
        silence_timeout_s=int(section.get("silence_timeout_s", 30)),
        max_call_length_s=int(section.get("max_call_length_s", 1800)),
    )


def _parse_outbound(data: dict[str, Any]) -> OutboundConfig:
    section = _section(data, "outbound")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        # A string like "false" would be truthy; refuse rather than guess.
        raise ConfigError("outbound.enabled must be true or false")

    quiet = _section(section, "quiet_hours")
    quiet_hours = QuietHours(
        start=str(quiet.get("start", "22:00")),
        end=str(quiet.get("end", "07:00")),
        timezone=str(quiet.get("timezone", "UTC")),
    )

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
        destination_number=str(section.get("destination_number", "")),
        max_calls_per_24h=int(section.get("max_calls_per_24h", 1)),
        quiet_hours=quiet_hours,
        event_types=event_types,
    )


def load_config_from_dict(data: dict[str, Any]) -> Config:
    """Validate a parsed config mapping and return a Config."""
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping")

    tts = _section(data, "tts")
    return Config(
        telephony_provider=str(data.get("telephony_provider", "telnyx")),
        agent_backend=str(data.get("agent_backend", "claude-agent-sdk")),
        stt=_parse_stt(data),
        tts=TtsConfig(
            engine=str(tts.get("engine", "kokoro")),
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
