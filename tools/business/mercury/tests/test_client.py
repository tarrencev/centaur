from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from centaur_sdk import ToolContext, reset_tool_context, set_tool_context  # noqa: E402

CLIENT_PATH = REPO_ROOT / "tools" / "business" / "mercury" / "client.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("test_mercury_client_module", CLIENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _mock_client(client, handler) -> None:
    client._client.close()
    client._client = httpx.Client(
        base_url=client.base_url,
        headers={"Authorization": f"Bearer {client.api_key}", "Accept": "application/json"},
        transport=httpx.MockTransport(handler),
    )


def test_factory_uses_secret_and_base_url_override() -> None:
    module = _load_module()
    token = set_tool_context(
        ToolContext(
            name="mercury",
            secrets={
                "MERCURY_API_KEY": " secret-token:test\nignored ",
                "MERCURY_API_BASE_URL": "https://api-sandbox.mercury.com/api/v1",
            },
        )
    )
    try:
        client = module._client()
        try:
            assert client.api_key == "secret-token:test"
            assert client.base_url == "https://api-sandbox.mercury.com/api/v1"
        finally:
            client.close()
    finally:
        reset_tool_context(token)


def test_get_accounts_sends_bearer_auth_and_pagination_params() -> None:
    module = _load_module()
    client = module.MercuryClient(api_key="secret-token:test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/accounts"
        assert request.headers["Authorization"] == "Bearer secret-token:test"
        assert request.url.params.get("limit") == "25"
        assert request.url.params.get("order") == "desc"
        return httpx.Response(200, request=request, json={"accounts": []})

    _mock_client(client, handler)
    try:
        assert client.get_accounts(limit=25, order="desc") == {"accounts": []}
    finally:
        client.close()


def test_send_money_posts_body_to_account_transactions() -> None:
    module = _load_module()
    client = module.MercuryClient(api_key="secret-token:test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/account/acct-123/transactions"
        assert request.content == b'{"amount":100,"recipientId":"rec-1"}'
        return httpx.Response(200, request=request, json={"id": "txn-1"})

    _mock_client(client, handler)
    try:
        result = client.send_money("acct-123", {"amount": 100, "recipientId": "rec-1"})
    finally:
        client.close()

    assert result == {"id": "txn-1"}


def test_error_messages_distinguish_auth_and_permission() -> None:
    module = _load_module()
    client = module.MercuryClient(api_key="secret-token:test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request, json={"message": "scope missing"})

    _mock_client(client, handler)
    try:
        with pytest.raises(RuntimeError, match="permission denied: scope missing"):
            client.get_accounts()
    finally:
        client.close()


def test_raw_request_rejects_absolute_urls() -> None:
    module = _load_module()
    client = module.MercuryClient(api_key="secret-token:test")
    try:
        with pytest.raises(ValueError, match="relative Mercury API path"):
            client.raw_request("GET", "https://api.mercury.com/api/v1/accounts")
    finally:
        client.close()


def test_detail_endpoints_match_mercury_plural_paths() -> None:
    module = _load_module()
    client = module.MercuryClient(api_key="secret-token:test")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, request=request, json={"ok": True})

    _mock_client(client, handler)
    try:
        assert client.get_user("usr-1") == {"ok": True}
        assert client.get_event("evt-1") == {"ok": True}
    finally:
        client.close()

    assert seen == ["/api/v1/users/usr-1", "/api/v1/events/evt-1"]


def test_download_statement_pdf_saves_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    saved = {}
    monkeypatch.setattr(module, "save_attachment", lambda **kwargs: saved | kwargs)
    client = module.MercuryClient(api_key="secret-token:test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/statements/st-1/pdf"
        return httpx.Response(
            200,
            request=request,
            content=b"%PDF",
            headers={"content-type": "application/pdf"},
        )

    _mock_client(client, handler)
    try:
        result = client.download_statement_pdf("st-1")
    finally:
        client.close()

    assert result["name"] == "mercury-statement-st-1.pdf"
    assert result["data"] == b"%PDF"
    assert result["mime_type"] == "application/pdf"


def test_upload_transaction_attachment_from_base64() -> None:
    module = _load_module()
    client = module.MercuryClient(api_key="secret-token:test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/transaction/txn-1/attachments"
        assert b"receipt.pdf" in request.content
        assert b"hello" in request.content
        return httpx.Response(200, request=request, json={"id": "att-1"})

    _mock_client(client, handler)
    try:
        result = client.upload_transaction_attachment(
            "txn-1",
            content_base64=base64.b64encode(b"hello").decode("ascii"),
            filename="receipt.pdf",
            mime_type="application/pdf",
        )
    finally:
        client.close()

    assert result == {"id": "att-1"}


def test_upload_requires_exactly_one_source() -> None:
    module = _load_module()
    client = module.MercuryClient(api_key="secret-token:test")
    try:
        with pytest.raises(ValueError, match="exactly one"):
            client.upload_transaction_attachment("txn-1")
    finally:
        client.close()
