import pytest

from agent_hotline.config import ConfigError, load_config, load_config_from_dict


def test_load_config_valid_example():
    config = load_config("config.example.yaml")
    assert config.inbound.allowed_numbers == ["+15551234567"]
    assert config.outbound.enabled is False
    assert config.stt.device == "cpu"


def test_load_config_missing_allowed_numbers_raises():
    # a config.yaml-shaped dict missing inbound.allowed_numbers
    with pytest.raises(ConfigError):
        load_config_from_dict({"inbound": {}, "outbound": {}})


def test_load_config_outbound_enabled_defaults_false_when_absent():
    config = load_config_from_dict({"inbound": {"allowed_numbers": ["+1"]}, "outbound": {}})
    assert config.outbound.enabled is False


def test_load_config_max_calls_per_24h_defaults_to_one_when_absent():
    config = load_config_from_dict({"inbound": {"allowed_numbers": ["+1"]}, "outbound": {"enabled": True}})
    assert config.outbound.max_calls_per_24h == 1


def test_load_config_stt_device_auto_rejected():
    with pytest.raises(ConfigError):
        load_config_from_dict({"inbound": {"allowed_numbers": ["+1"]}, "stt": {"device": "auto"}})
