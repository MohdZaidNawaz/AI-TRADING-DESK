import ast
from pathlib import Path

from app.audit.models import AuditEvent
from app.observability.events import StructuredEvent


def test_sensitive_values_are_rejected_or_redacted_from_new_hardening_contracts() -> None:
    assert "password" not in repr(StructuredEvent.model_fields)
    try:
        AuditEvent(event_type="failure", occurred_at="2026-01-01T00:00:00Z", details=(("token", "secret"),))
    except ValueError:
        pass
    else:
        raise AssertionError("audit details accepted a sensitive key")


def test_no_runtime_mt5_submission_or_broker_bypass_exists() -> None:
    source_root = Path(__file__).resolve().parents[1] / "app"
    runtime_sources = [path for path in source_root.rglob("*.py") if "tests" not in path.parts]
    calls = [
        node.func
        for path in runtime_sources
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert not any(call.attr == "order_send" for call in calls)
    assert not any(
        call.attr == "submit" and isinstance(call.value, ast.Name) and call.value.id == "adapter"
        for call in calls
    )
