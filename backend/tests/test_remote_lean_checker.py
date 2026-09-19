import httpx

from app.services.lean_checker import RemoteLeanChecker

NAME = "remote_ok"
SIG = ": 2 + 2 = 4"
SOURCE = f"theorem {NAME} {SIG} := rfl"


def response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://checker"))


def test_remote_checker_maps_verified_result(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_post(*_args, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["headers"] == {"X-Checker-Token": "secret"}
        assert "allowed_axioms" not in kwargs["json"]
        return response(
            200,
            {
                "status": "verified",
                "reasons": [],
                "axioms": [],
                "toolchain": "Lean 4.24.0",
                "declaration": NAME,
                "content_hash": "abc",
                "log_tail": "does not depend on any axioms",
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    result = RemoteLeanChecker("http://checker", token="secret").check(SOURCE, NAME, SIG)
    assert result.status == "verified"
    assert result.toolchain == "Lean 4.24.0"
    assert result.axioms == []


def test_remote_checker_fails_closed_on_network_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fail(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "post", fail)
    result = RemoteLeanChecker("http://checker", token="secret").check(SOURCE, NAME, SIG)
    assert result.status == "checker_unavailable"
    assert "offline" in result.reasons[0]


def test_remote_checker_applies_static_policy_before_network(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def unexpected(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("forbidden source must not be sent to the checker service")

    monkeypatch.setattr(httpx, "post", unexpected)
    result = RemoteLeanChecker("http://checker", token="secret").check(
        f"theorem {NAME} {SIG} := by sorry", NAME, SIG
    )
    assert result.status == "rejected"
    assert any("sorry" in reason for reason in result.reasons)
