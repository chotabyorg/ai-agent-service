from mcp_clients.servers.google_workspace.utils.responses import google_api_error_response


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakeGoogleError(Exception):
    def __init__(self, status: int, content: bytes) -> None:
        super().__init__("raw google error with secret-token@example.com")
        self.resp = _FakeResponse(status)
        self.content = content


def test_google_api_error_response_maps_invalid_token_without_leaking_body() -> None:
    response = google_api_error_response(
        _FakeGoogleError(
            401,
            b'{"error":{"status":"UNAUTHENTICATED","message":"invalid secret-token@example.com"}}',
        ),
        "Gmail",
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "GOOGLE_AUTH_INVALID"
    assert response["error"]["recoverable"] is True
    assert response["error"]["details"] == {
        "type": "_FakeGoogleError",
        "status": 401,
        "reason": "UNAUTHENTICATED",
    }
    assert "secret-token@example.com" not in str(response)


def test_google_api_error_response_maps_rate_limit() -> None:
    response = google_api_error_response(
        _FakeGoogleError(429, b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'),
        "Google Drive",
    )

    assert response["error"]["code"] == "GOOGLE_RATE_LIMITED"
    assert response["error"]["details"]["status"] == 429


def test_google_api_error_response_keeps_generic_errors_sanitized() -> None:
    response = google_api_error_response(RuntimeError("contains private user data"), "Google Contacts")

    assert response["error"]["code"] == "GOOGLE_API_ERROR"
    assert response["error"]["message"] == "Google Contacts request failed."
    assert response["error"]["details"] == {"type": "RuntimeError"}
    assert "private user data" not in str(response)
