from app.services.tool_payloads import MAX_TOOL_PAYLOAD_BYTES, safe_tool_payload


def test_tool_payload_redacts_named_and_embedded_credentials():
    value, truncated = safe_tool_payload(
        {"api_key": "provider-key", "nested": {"Authorization": "Bearer provider-key"}, "echo": "shared-secret"},
        ("provider-key", "shared-secret"),
    )

    assert not truncated
    assert value == {"api_key": "[REDACTED]", "nested": {"Authorization": "[REDACTED]"}, "echo": "[REDACTED]"}


def test_tool_payload_truncation_keeps_safe_preview_and_original_bytes():
    value, truncated = safe_tool_payload({"result": "provider-key" * MAX_TOOL_PAYLOAD_BYTES}, ("provider-key",))

    assert truncated
    assert value["original_bytes"] > MAX_TOOL_PAYLOAD_BYTES
    assert "provider-key" not in value["preview"]
