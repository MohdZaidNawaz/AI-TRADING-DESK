from pathlib import Path


def test_phase_ten_has_no_credentials_raw_payloads_or_live_submission_path() -> None:
    root = Path(__file__).resolve().parents[1]
    gate = (root / "app/services/execution_gate.py").read_text(encoding="utf-8").lower()
    pilot = (root / "app/execution/pilot.py").read_text(encoding="utf-8").lower()
    state = (root / "app/models/execution_state.py").read_text(encoding="utf-8").lower()
    assert "self._adapter.submit(" not in gate
    assert "metatrader" not in pilot
    assert "self._adapter.prepare(" not in pilot
    assert "password" not in state and "api_key" not in state and "raw_payload" not in state


def test_phase_ten_creates_no_public_order_endpoint() -> None:
    root = Path(__file__).resolve().parents[1]
    route_text = "\n".join(path.read_text(encoding="utf-8").lower() for path in (root / "app/api/routes").glob("*.py"))
    assert "execution" not in route_text and "order" not in route_text
