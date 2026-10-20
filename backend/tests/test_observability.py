from datetime import UTC, datetime
import logging

from app.observability.events import StructuredEvent, log_event


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
