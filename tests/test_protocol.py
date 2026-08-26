from __future__ import annotations

from meshdock.bridge.protocol import redact


def test_redaction_covers_secret_fields_bearers_and_url_queries() -> None:
    value = {
        "api_key": "canary-secret",
        "access_token": "access-canary",
        "tokenhub": {"model": "tripo-3d-3.1"},
        "message": "Authorization failed for Bearer abc.def.ghi",
        "url": "https://provider.example/download/model.glb?signature=secret",
    }
    cleaned = redact(value)
    rendered = str(cleaned)
    assert "canary-secret" not in rendered
    assert "abc.def.ghi" not in rendered
    assert "signature=secret" not in rendered
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["access_token"] == "[REDACTED]"
    assert cleaned["tokenhub"] == {"model": "tripo-3d-3.1"}
