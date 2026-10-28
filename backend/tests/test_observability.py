from datetime import UTC, datetime
import logging

import pytest

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


def test_every_top_level_identifier_redacts_secret_looking_values(caplog) -> None:
    sensitive = "synthetic-secret-fixture"
    fields = (
        "component", "event_type", "correlation_id", "proposal_id", "analysis_id",
        "intent_id", "execution_id", "approval_id", "provider", "symbol",
        "account_reference", "state", "reason_code",
    )
    for field in fields:
        values = {
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "component": "audit",
            "event_type": "security-check",
            field: f"token={sensitive}",
        }
        event = StructuredEvent(**values)
        assert sensitive not in str(event.model_dump(mode="json"))
        caplog.clear()
        caplog.set_level(logging.INFO, logger="friday")
        log_event(event)
        assert sensitive not in caplog.text
        assert "[REDACTED]" in caplog.text


def test_log_event_rejects_arbitrary_objects_without_invoking_serializers(caplog) -> None:
    class ArbitraryEvent:
        severity = "INFO"

        def __init__(self) -> None:
            self.calls = 0

        def model_dump(self, **_kwargs):
            self.calls += 1
            return {"secret": "must-not-appear"}

    event = ArbitraryEvent()
    caplog.set_level(logging.INFO, logger="friday")

    with pytest.raises(ValueError, match="exact event type"):
        log_event(event)  # type: ignore[arg-type]

    assert event.calls == 0
    assert "must-not-appear" not in caplog.text


def test_log_event_never_invokes_shadowed_model_dump(caplog) -> None:
    calls: list[bool] = []

    def shadowed_model_dump(**_kwargs):
        calls.append(True)
        return {"secret": "must-not-appear"}

    event = StructuredEvent(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        component="audit",
        event_type="write",
    ).model_copy(update={"model_dump": shadowed_model_dump})
    caplog.set_level(logging.INFO, logger="friday")

    with pytest.raises(ValueError, match="fields do not match"):
        log_event(event)

    assert calls == []
    assert "must-not-appear" not in caplog.text


def test_log_event_revalidates_copied_state_and_redacts_sensitive_values(caplog) -> None:
    sensitive = "copied-sensitive-fixture"
    event = StructuredEvent(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        component="audit",
        event_type="write",
    ).model_copy(
        update={
            "component": f"token={sensitive}",
            "fields": (("detail", f"password={sensitive}"),),
        }
    )
    caplog.set_level(logging.INFO, logger="friday")

    log_event(event)

    assert sensitive not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_log_event_rejects_invalid_and_unexpected_extra_state(caplog) -> None:
    base = StructuredEvent(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        component="audit",
        event_type="write",
    )
    invalid = base.model_copy(update={"severity": "TRACE"})
    extra = base.model_copy()
    object.__setattr__(extra, "__pydantic_extra__", {"unexpected": "token=hidden"})
    caplog.set_level(logging.INFO, logger="friday")

    with pytest.raises(ValueError):
        log_event(invalid)
    with pytest.raises(ValueError, match="undeclared extra state"):
        log_event(extra)

    assert "hidden" not in caplog.text
