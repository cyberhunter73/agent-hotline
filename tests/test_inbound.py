import pytest

from agent_hotline.inbound import InboundGate, normalize_number


def test_normalize_strips_formatting():
    assert normalize_number("(555) 123-4567") == "+15551234567"

def test_normalize_adds_country_code_when_missing():
    assert normalize_number("5551234567") == "+15551234567"

def test_normalize_leaves_already_normalized_number():
    assert normalize_number("+15551234567") == "+15551234567"

def test_allowed_number_passes_regardless_of_format():
    gate = InboundGate(allowed_numbers=["+15551234567"])
    assert gate.is_allowed("(555) 123-4567") is True

def test_unlisted_number_rejected():
    gate = InboundGate(allowed_numbers=["+15551234567"])
    assert gate.is_allowed("+15559999999") is False

def test_allowlist_entry_in_local_format_matches_e164_caller():
    # Normalization must apply to the stored entries too, not only the caller.
    gate = InboundGate(allowed_numbers=["(555) 123-4567"])
    assert gate.is_allowed("+15551234567") is True

def test_eleven_digit_number_with_leading_one_gets_plus():
    assert normalize_number("1-555-123-4567") == "+15551234567"

@pytest.mark.parametrize("garbage", ["", "   ", "not a number", "555-CALL-NOW", "555123", "1555123456789012345", "+", "+1 555 abc 4567"])
def test_normalize_rejects_garbage(garbage):
    with pytest.raises(ValueError):
        normalize_number(garbage)

def test_gate_rejects_caller_with_garbage_number():
    gate = InboundGate(allowed_numbers=["+15551234567"])
    assert gate.is_allowed("") is False
    assert gate.is_allowed("555-CALL-NOW") is False

def test_gate_raises_on_invalid_allowlist_entry():
    # A misconfigured allowlist entry must fail loudly, never be silently dropped.
    with pytest.raises(ValueError):
        InboundGate(allowed_numbers=["not-a-number"])
