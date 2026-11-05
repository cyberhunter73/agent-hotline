import pytest

from agent_hotline.config import ConfigError, load_config, load_config_from_dict

# M1 fix-wave note: "+1" (used in the original brief's test snippets) is not
# a valid phone number under agent_hotline.inbound.normalize_number (too few
# digits for an international number) and inbound.allowed_numbers entries are
# now validated through that function at config LOAD time (see config.py).
# These fixtures use a real-shaped number instead, same as test_worker.py
# already does for the same reason — preserving each test's actual intent
# (what it is really checking) without tripping the new validation.
_VALID_NUMBER = "+15551234567"


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
    config = load_config_from_dict({"inbound": {"allowed_numbers": [_VALID_NUMBER]}, "outbound": {}})
    assert config.outbound.enabled is False


def test_load_config_max_calls_per_24h_defaults_to_one_when_absent():
    config = load_config_from_dict(
        {
            "inbound": {"allowed_numbers": [_VALID_NUMBER]},
            "outbound": {"enabled": True, "destination_number": "+15557654321"},
        }
    )
    assert config.outbound.max_calls_per_24h == 1


def test_load_config_stt_device_auto_rejected():
    with pytest.raises(ConfigError):
        load_config_from_dict({"inbound": {"allowed_numbers": [_VALID_NUMBER]}, "stt": {"device": "auto"}})


# --- M1: config validation sweep -------------------------------------------------


def test_load_config_invalid_allowed_number_entry_raises():
    with pytest.raises(ConfigError):
        load_config_from_dict({"inbound": {"allowed_numbers": ["not-a-number"]}})


def test_load_config_silence_timeout_non_integer_raises_config_error_not_value_error():
    with pytest.raises(ConfigError):
        load_config_from_dict(
            {"inbound": {"allowed_numbers": [_VALID_NUMBER], "silence_timeout_s": "soon"}}
        )


def test_load_config_silence_timeout_zero_raises():
    with pytest.raises(ConfigError):
        load_config_from_dict({"inbound": {"allowed_numbers": [_VALID_NUMBER], "silence_timeout_s": 0}})


def test_load_config_max_call_length_negative_raises():
    with pytest.raises(ConfigError):
        load_config_from_dict({"inbound": {"allowed_numbers": [_VALID_NUMBER], "max_call_length_s": -5}})


def test_load_config_max_calls_per_24h_zero_raises():
    with pytest.raises(ConfigError):
        load_config_from_dict(
            {
                "inbound": {"allowed_numbers": [_VALID_NUMBER]},
                "outbound": {"max_calls_per_24h": 0},
            }
        )


def test_load_config_outbound_enabled_requires_destination_number():
    with pytest.raises(ConfigError):
        load_config_from_dict(
            {"inbound": {"allowed_numbers": [_VALID_NUMBER]}, "outbound": {"enabled": True}}
        )


def test_load_config_outbound_enabled_rejects_invalid_destination_number():
    with pytest.raises(ConfigError):
        load_config_from_dict(
            {
                "inbound": {"allowed_numbers": [_VALID_NUMBER]},
                "outbound": {"enabled": True, "destination_number": "garbage"},
            }
        )


def test_load_config_outbound_disabled_does_not_require_destination_number():
    config = load_config_from_dict(
        {"inbound": {"allowed_numbers": [_VALID_NUMBER]}, "outbound": {"enabled": False}}
    )
    assert config.outbound.destination_number == ""


def test_load_config_bad_quiet_hours_hhmm_raises():
    with pytest.raises(ConfigError):
        load_config_from_dict(
            {
                "inbound": {"allowed_numbers": [_VALID_NUMBER]},
                "outbound": {"quiet_hours": {"start": "not-a-time"}},
            }
        )


def test_load_config_quiet_hours_hour_out_of_range_raises():
    with pytest.raises(ConfigError):
        load_config_from_dict(
            {
                "inbound": {"allowed_numbers": [_VALID_NUMBER]},
                "outbound": {"quiet_hours": {"start": "25:00"}},
            }
        )


def test_load_config_bad_quiet_hours_timezone_raises():
    with pytest.raises(ConfigError):
        load_config_from_dict(
            {
                "inbound": {"allowed_numbers": [_VALID_NUMBER]},
                "outbound": {"quiet_hours": {"timezone": "Not/AZone"}},
            }
        )


def test_load_config_valid_quiet_hours_timezone_accepted():
    config = load_config_from_dict(
        {
            "inbound": {"allowed_numbers": [_VALID_NUMBER]},
            "outbound": {"quiet_hours": {"timezone": "America/New_York"}},
        }
    )
    assert config.outbound.quiet_hours.timezone == "America/New_York"


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("telephony_provider", "twilio"),
        ("agent_backend", "langchain"),
    ],
)
def test_load_config_rejects_unimplemented_top_level_choice(field, bad_value):
    with pytest.raises(ConfigError):
        load_config_from_dict({"inbound": {"allowed_numbers": [_VALID_NUMBER]}, field: bad_value})


def test_load_config_rejects_unimplemented_stt_engine():
    with pytest.raises(ConfigError):
        load_config_from_dict(
            {
                "inbound": {"allowed_numbers": [_VALID_NUMBER]},
                "stt": {"engine": "deepgram", "device": "cpu"},
            }
        )


def test_load_config_rejects_unimplemented_tts_engine():
    with pytest.raises(ConfigError):
        load_config_from_dict(
            {
                "inbound": {"allowed_numbers": [_VALID_NUMBER]},
                "tts": {"engine": "elevenlabs"},
            }
        )
