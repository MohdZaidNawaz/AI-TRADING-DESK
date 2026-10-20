from datetime import UTC, datetime
import logging

from app.observability.events import StructuredEvent, log_event, safe_fields


def test_structured_logs_redact_sensitive_fields(caplog) -> None:
    event = StructuredEvent(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC), component="audit", event_type="write",
        fields=(
            ("safe", "value"),
            ("api_key", "must-not-appear"),
            ("password", "must-not-appear"),
            ("detail", "token=must-not-appear"),
            ("message", "password:must-not-appear"),
        ),
    )
    caplog.set_level(logging.INFO, logger="friday")
    log_event(event)
    output = caplog.text
    assert "must-not-appear" not in output
    assert '"fields":[["safe","value"]]' in output


def test_structured_logs_reject_nested_quoted_and_authorization_values(caplog) -> None:
    sensitive = "nested-sensitive-fixture"
    event = StructuredEvent(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        component="audit",
        event_type="nested-write",
        fields=safe_fields(
            {
                "safe": "retained",
                "nested": {"password": sensitive},
                "json": f'{{"api_key":"{sensitive}"}}',
                "header": f"Bearer {sensitive}",
                "pem": "-----BEGIN PRIVATE KEY-----",
            }
        ),
    )
    caplog.set_level(logging.INFO, logger="friday")

    log_event(event)

    output = caplog.text
    assert sensitive not in output
    assert "PRIVATE KEY" not in output
    assert '"fields":[["safe","retained"]]' in output
