"""Mercury Banking API client."""

from __future__ import annotations

import base64
import contextlib
import mimetypes
import urllib.request
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from centaur_sdk import current_thread_key, save_attachment, secret

DEFAULT_BASE_URL = "https://api.mercury.com/api/v1"


def _clean(value: str | None) -> str:
    stripped = (value or "").strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0].strip()


def _path_part(value: str) -> str:
    return quote(value, safe="")


def _params(**kwargs: Any) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if value is not None}


class MercuryClient:
    """Client for Mercury's REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
    ):
        self.api_key = _clean(api_key) or _clean(secret("MERCURY_API_KEY", ""))
        if not self.api_key:
            raise RuntimeError("MERCURY_API_KEY not set.")

        configured_base = _clean(base_url) or _clean(secret("MERCURY_API_BASE_URL", ""))
        if not configured_base or configured_base == "MERCURY_API_BASE_URL":
            configured_base = DEFAULT_BASE_URL
        self.base_url = configured_base.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        binary: bool = False,
    ) -> Any:
        response = self._client.request(
            method,
            path,
            params=params,
            json=json,
            files=files,
            data=data,
        )
        if response.status_code >= 400:
            message = response.text
            with contextlib.suppress(Exception):
                payload = response.json()
                if isinstance(payload, dict):
                    message = str(
                        payload.get("message")
                        or payload.get("error")
                        or payload.get("details")
                        or payload
                    )
            if response.status_code == 401:
                raise RuntimeError(f"Mercury API authentication failed: {message}")
            if response.status_code == 403:
                raise RuntimeError(f"Mercury API permission denied: {message}")
            raise RuntimeError(f"Mercury API error {response.status_code}: {message}")

        if binary:
            return response
        if not response.content:
            return {}
        return response.json()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json=body or {})

    def _patch(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("PATCH", path, json=body or {})

    def _delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def _download_pdf(self, path: str, filename: str, source_url: str) -> dict[str, Any]:
        response = self._request("GET", path, binary=True)
        content_type = response.headers.get("content-type") or "application/pdf"
        return save_attachment(
            name=filename,
            mime_type=content_type,
            data=response.content,
            source_url=source_url,
        )

    def _download_attachment_bytes(
        self,
        *,
        attachment_id: str | None = None,
        attachment_url: str | None = None,
    ) -> bytes:
        path = attachment_url
        if attachment_id:
            path = f"/agent/attachments/{attachment_id}/download"
        if not path:
            raise ValueError("attachment_id or attachment_url is required")

        base_url = secret("CENTAUR_API_URL", "http://api:8000").rstrip("/")
        base_parts = urlparse(base_url)
        if path.startswith(("http://", "https://")):
            url_parts = urlparse(path)
            if (url_parts.scheme, url_parts.netloc) != (base_parts.scheme, base_parts.netloc):
                raise ValueError("attachment_url must point at the configured Centaur API")
            url = path
        else:
            if not path.startswith("/agent/attachments/"):
                raise ValueError("attachment_url must be a Centaur attachment download path")
            url = urljoin(f"{base_url}/", path.lstrip("/"))

        sep = "&" if urlparse(url).query else "?"
        url = f"{url}{sep}thread_key={quote(current_thread_key(), safe='')}"

        headers: dict[str, str] = {}
        api_key = secret("CENTAUR_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def _upload_file(
        self,
        path: str,
        *,
        content_base64: str | None = None,
        attachment_id: str | None = None,
        attachment_url: str | None = None,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> Any:
        sources = [
            content_base64 is not None,
            attachment_id is not None,
            attachment_url is not None,
        ]
        if sum(sources) != 1:
            raise ValueError(
                "Provide exactly one of content_base64, attachment_id, or attachment_url"
            )

        if content_base64 is not None:
            data = base64.b64decode(content_base64)
        else:
            data = self._download_attachment_bytes(
                attachment_id=attachment_id,
                attachment_url=attachment_url,
            )
        effective_filename = filename or f"{attachment_id or 'attachment'}.bin"
        effective_mime = (
            mime_type or mimetypes.guess_type(effective_filename)[0] or "application/octet-stream"
        )
        files = {"file": (effective_filename, data, effective_mime)}
        return self._request("POST", path, files=files)

    # Accounts and payments
    def get_accounts(
        self,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """Get all accounts."""
        return self._get(
            "/accounts",
            _params(limit=limit, order=order, start_after=start_after, end_before=end_before),
        )

    def get_account(self, account_id: str) -> dict[str, Any]:
        """Get account by ID."""
        return self._get(f"/account/{_path_part(account_id)}")

    def get_account_cards(self, account_id: str) -> dict[str, Any]:
        """Get cards for an account."""
        return self._get(f"/account/{_path_part(account_id)}/cards")

    def get_account_statements(
        self,
        account_id: str,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """Get account statements."""
        return self._get(
            f"/account/{_path_part(account_id)}/statements",
            _params(
                start=start,
                end=end,
                limit=limit,
                order=order,
                start_after=start_after,
                end_before=end_before,
            ),
        )

    def download_statement_pdf(
        self, statement_id: str, filename: str | None = None
    ) -> dict[str, Any]:
        """Download an account statement PDF into a Centaur attachment."""
        path = f"/statements/{_path_part(statement_id)}/pdf"
        return self._download_pdf(
            path,
            filename or f"mercury-statement-{statement_id}.pdf",
            f"{self.base_url}{path}",
        )

    def list_account_transactions(
        self,
        account_id: str,
        start: str | None = None,
        end: str | None = None,
        status: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """List transactions for one account."""
        return self._get(
            f"/account/{_path_part(account_id)}/transactions",
            _params(
                start=start,
                end=end,
                status=status,
                search=search,
                limit=limit,
                order=order,
                start_after=start_after,
                end_before=end_before,
            ),
        )

    def get_transaction(self, account_id: str, transaction_id: str) -> dict[str, Any]:
        """Get an account transaction by ID."""
        return self._get(
            f"/account/{_path_part(account_id)}/transaction/{_path_part(transaction_id)}"
        )

    def send_money(self, account_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Send money from an account to a recipient."""
        return self._post(f"/account/{_path_part(account_id)}/transactions", body)

    def request_send_money(self, account_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create a send-money approval request."""
        return self._post(f"/account/{_path_part(account_id)}/request-send-money", body)

    def create_internal_transfer(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create an internal transfer."""
        return self._post("/transfer", body)

    def list_send_money_approval_requests(
        self,
        account_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """List send-money approval requests."""
        return self._get(
            "/request-send-money",
            _params(
                accountId=account_id,
                status=status,
                limit=limit,
                order=order,
                start_after=start_after,
                end_before=end_before,
            ),
        )

    def get_send_money_approval_request(self, request_id: str) -> dict[str, Any]:
        """Get a send-money approval request by ID."""
        return self._get(f"/request-send-money/{_path_part(request_id)}")

    # Transactions and categories
    def list_transactions(
        self,
        start: str | None = None,
        end: str | None = None,
        status: str | None = None,
        search: str | None = None,
        category_id: str | None = None,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """List transactions across all accounts."""
        return self._get(
            "/transactions",
            _params(
                start=start,
                end=end,
                status=status,
                search=search,
                categoryId=category_id,
                limit=limit,
                order=order,
                start_after=start_after,
                end_before=end_before,
            ),
        )

    def get_transaction_by_id(self, transaction_id: str) -> dict[str, Any]:
        """Get a transaction by ID."""
        return self._get(f"/transaction/{_path_part(transaction_id)}")

    def update_transaction(self, transaction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Update transaction metadata such as note or category."""
        return self._patch(f"/transaction/{_path_part(transaction_id)}", body)

    def upload_transaction_attachment(
        self,
        transaction_id: str,
        content_base64: str | None = None,
        attachment_id: str | None = None,
        attachment_url: str | None = None,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload an attachment to a transaction."""
        return self._upload_file(
            f"/transaction/{_path_part(transaction_id)}/attachments",
            content_base64=content_base64,
            attachment_id=attachment_id,
            attachment_url=attachment_url,
            filename=filename,
            mime_type=mime_type,
        )

    def list_categories(
        self,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """List custom expense categories."""
        return self._get(
            "/categories",
            _params(limit=limit, order=order, start_after=start_after, end_before=end_before),
        )

    def create_category(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create a custom expense category."""
        return self._post("/categories", body)

    def edit_category(self, expense_category_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Edit a custom expense category."""
        return self._patch(f"/categories/{_path_part(expense_category_id)}", body)

    def delete_category(self, expense_category_id: str) -> dict[str, Any]:
        """Delete a custom expense category."""
        return self._delete(f"/categories/{_path_part(expense_category_id)}")

    # Recipients
    def get_recipients(
        self,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """Get all recipients."""
        return self._get(
            "/recipients",
            _params(limit=limit, order=order, start_after=start_after, end_before=end_before),
        )

    def get_recipient(self, recipient_id: str) -> dict[str, Any]:
        """Get recipient by ID."""
        return self._get(f"/recipient/{_path_part(recipient_id)}")

    def create_recipient(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create a recipient."""
        return self._post("/recipients", body)

    def update_recipient(self, recipient_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Update a recipient."""
        return self._patch(f"/recipient/{_path_part(recipient_id)}", body)

    def list_recipient_attachments(
        self,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """List recipient tax-form attachments."""
        return self._get(
            "/recipients/attachments",
            _params(limit=limit, order=order, start_after=start_after, end_before=end_before),
        )

    def upload_recipient_attachment(
        self,
        recipient_id: str,
        content_base64: str | None = None,
        attachment_id: str | None = None,
        attachment_url: str | None = None,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload a tax-form attachment for a recipient."""
        return self._upload_file(
            f"/recipient/{_path_part(recipient_id)}/attachments",
            content_base64=content_base64,
            attachment_id=attachment_id,
            attachment_url=attachment_url,
            filename=filename,
            mime_type=mime_type,
        )

    # Organization, users, events, credit, treasury
    def get_organization(self) -> dict[str, Any]:
        """Get organization information."""
        return self._get("/organization")

    def get_users(
        self,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """Get all users."""
        return self._get(
            "/users",
            _params(limit=limit, order=order, start_after=start_after, end_before=end_before),
        )

    def get_user(self, user_id: str) -> dict[str, Any]:
        """Get user by ID."""
        return self._get(f"/users/{_path_part(user_id)}")

    def get_events(
        self,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """Get all events."""
        return self._get(
            "/events",
            _params(limit=limit, order=order, start_after=start_after, end_before=end_before),
        )

    def get_event(self, event_id: str) -> dict[str, Any]:
        """Get event by ID."""
        return self._get(f"/events/{_path_part(event_id)}")

    def list_credit(self) -> dict[str, Any]:
        """List credit accounts."""
        return self._get("/credit")

    def get_treasury(
        self,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """Get treasury accounts."""
        return self._get(
            "/treasury",
            _params(limit=limit, order=order, start_after=start_after, end_before=end_before),
        )

    def get_treasury_transactions(
        self,
        treasury_id: str,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """Get treasury transactions."""
        return self._get(
            f"/treasury/{_path_part(treasury_id)}/transactions",
            _params(
                start=start,
                end=end,
                limit=limit,
                order=order,
                start_after=start_after,
                end_before=end_before,
            ),
        )

    def get_treasury_statements(
        self,
        treasury_id: str,
        document_type: str | None = None,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """Get treasury statements."""
        return self._get(
            f"/treasury/{_path_part(treasury_id)}/statements",
            _params(
                documentType=document_type,
                limit=limit,
                order=order,
                start_after=start_after,
                end_before=end_before,
            ),
        )

    # Accounts receivable
    def list_customers(
        self,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """List accounts receivable customers."""
        return self._get(
            "/ar/customers",
            _params(limit=limit, order=order, start_after=start_after, end_before=end_before),
        )

    def create_customer(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create an accounts receivable customer."""
        return self._post("/ar/customers", body)

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        """Get accounts receivable customer by ID."""
        return self._get(f"/ar/customers/{_path_part(customer_id)}")

    def update_customer(self, customer_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Update an accounts receivable customer."""
        return self._patch(f"/ar/customers/{_path_part(customer_id)}", body)

    def delete_customer(self, customer_id: str) -> dict[str, Any]:
        """Delete an accounts receivable customer."""
        return self._delete(f"/ar/customers/{_path_part(customer_id)}")

    def list_invoices(
        self,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """List invoices."""
        return self._get(
            "/ar/invoices",
            _params(limit=limit, order=order, start_after=start_after, end_before=end_before),
        )

    def create_invoice(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create an invoice."""
        return self._post("/ar/invoices", body)

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        """Get invoice by ID."""
        return self._get(f"/ar/invoices/{_path_part(invoice_id)}")

    def update_invoice(self, invoice_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Update an invoice."""
        return self._patch(f"/ar/invoices/{_path_part(invoice_id)}", body)

    def cancel_invoice(self, invoice_id: str) -> dict[str, Any]:
        """Cancel an invoice."""
        return self._post(f"/ar/invoices/{_path_part(invoice_id)}/cancel")

    def download_invoice_pdf(self, invoice_id: str, filename: str | None = None) -> dict[str, Any]:
        """Download an invoice PDF into a Centaur attachment."""
        path = f"/ar/invoices/{_path_part(invoice_id)}/pdf"
        return self._download_pdf(
            path,
            filename or f"mercury-invoice-{invoice_id}.pdf",
            f"{self.base_url}{path}",
        )

    def list_invoice_attachments(self, invoice_id: str) -> dict[str, Any]:
        """List invoice attachments."""
        return self._get(f"/ar/invoices/{_path_part(invoice_id)}/attachments")

    def get_attachment(self, attachment_id: str) -> dict[str, Any]:
        """Get accounts receivable attachment details."""
        return self._get(f"/ar/attachments/{_path_part(attachment_id)}")

    def get_safe_requests(self) -> dict[str, Any]:
        """Get all SAFE requests."""
        return self._get("/safes")

    def get_safe_request(self, safe_request_id: str) -> dict[str, Any]:
        """Get SAFE request by ID."""
        return self._get(f"/safes/{_path_part(safe_request_id)}")

    def download_safe_document(
        self, safe_request_id: str, filename: str | None = None
    ) -> dict[str, Any]:
        """Download a SAFE document into a Centaur attachment."""
        path = f"/safes/{_path_part(safe_request_id)}/document"
        return self._download_pdf(
            path,
            filename or f"mercury-safe-{safe_request_id}.pdf",
            f"{self.base_url}{path}",
        )

    def submit_onboarding_data(self, body: dict[str, Any]) -> dict[str, Any]:
        """Submit onboarding data for an applicant."""
        return self._post("/submit-onboarding-data", body)

    # Webhooks
    def get_webhooks(
        self,
        status: str | None = None,
        limit: int | None = None,
        order: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
    ) -> dict[str, Any]:
        """Get webhook endpoints."""
        return self._get(
            "/webhooks",
            _params(
                status=status,
                limit=limit,
                order=order,
                start_after=start_after,
                end_before=end_before,
            ),
        )

    def get_webhook(self, webhook_endpoint_id: str) -> dict[str, Any]:
        """Get webhook endpoint by ID."""
        return self._get(f"/webhooks/{_path_part(webhook_endpoint_id)}")

    def create_webhook(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create a webhook endpoint."""
        return self._post("/webhooks", body)

    def update_webhook(self, webhook_endpoint_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Update a webhook endpoint."""
        return self._patch(f"/webhooks/{_path_part(webhook_endpoint_id)}", body)

    def delete_webhook(self, webhook_endpoint_id: str) -> dict[str, Any]:
        """Delete a webhook endpoint."""
        return self._delete(f"/webhooks/{_path_part(webhook_endpoint_id)}")

    def verify_webhook(
        self,
        webhook_endpoint_id: str,
        event_type: str | None = None,
    ) -> dict[str, Any]:
        """Verify a webhook endpoint."""
        body = {"eventType": event_type} if event_type else {}
        return self._post(f"/webhooks/{_path_part(webhook_endpoint_id)}/verify", body)

    def raw_request(
        self,
        method: str,
        endpoint: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Make a raw Mercury API request relative to `/api/v1`."""
        path = endpoint
        parsed = urlparse(endpoint)
        if parsed.scheme or parsed.netloc:
            raise ValueError("endpoint must be a relative Mercury API path")
        if not path.startswith("/"):
            path = f"/{path}"
        if path.startswith("/api/v1/"):
            path = path[len("/api/v1") :]
        return self._request(method.upper(), path, json=body, params=params)

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self) -> MercuryClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _client() -> MercuryClient:
    return MercuryClient()
