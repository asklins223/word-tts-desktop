import pytest

from workflow.data_safety import DataSafetyError, redact_public_json, validate_public_object


def test_browser_diagnostic_redacts_embedded_secret_but_keeps_context():
    value = redact_public_json(
        "browser launch failed: token=SECRET123 executable=/opt/Chrome/chrome"
    )

    assert value == (
        "browser launch failed: token=[REDACTED] executable=/opt/Chrome/chrome"
    )
    assert "SECRET123" not in value


def test_non_browser_sensitive_text_keeps_existing_whole_value_redaction():
    assert redact_public_json("token=SECRET123") == "[REDACTED]"


def test_user_config_still_rejects_sensitive_members():
    with pytest.raises(DataSafetyError):
        validate_public_object({"api_key": "SECRET123"})
