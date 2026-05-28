"""Typefully Public API v2 client."""

from __future__ import annotations

from typing import Any

import httpx

from centaur_sdk import secret

TYPEFULLY_BASE_URL = "https://api.typefully.com"

ALLOWED_DRAFT_STATUSES = {"draft", "published", "scheduled", "error", "publishing"}
ALLOWED_DRAFT_ORDER_BY = {
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "scheduled_date",
    "-scheduled_date",
    "published_at",
    "-published_at",
}
ALLOWED_PLATFORMS = {"x", "linkedin", "mastodon", "threads", "bluesky"}


def _remove_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _remove_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_remove_none(item) for item in value]
    return value


def _parse_optional_int(value: int | str | None, *, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


class TypefullyClient:
    """Client for Typefully's Public API v2."""

    def __init__(
        self,
        api_key: str | None = None,
        default_social_set_id: int | str | None = None,
        base_url: str = TYPEFULLY_BASE_URL,
        timeout: float = 30.0,
    ):
        self._api_key = api_key
        self._default_social_set_id = default_social_set_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._http: httpx.Client | None = None

    def _get_api_key(self) -> str:
        api_key = self._api_key or secret("TYPEFULLY_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "TYPEFULLY_API_KEY not set. Generate one at "
                "https://typefully.com/?settings=api"
            )
        return api_key

    def _get_default_social_set_id(self) -> int | None:
        configured = self._default_social_set_id
        if configured is None:
            configured = secret("TYPEFULLY_DEFAULT_SOCIAL_SET_ID", "")
        if configured == "TYPEFULLY_DEFAULT_SOCIAL_SET_ID":
            return None
        return _parse_optional_int(configured, name="TYPEFULLY_DEFAULT_SOCIAL_SET_ID")

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self._get_api_key()}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        return self._http

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_body = _remove_none(json_body) if json_body is not None else None
        clean_params = _remove_none(params) if params is not None else None
        try:
            response = self.http.request(method, path, json=clean_body, params=clean_params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(self._format_http_error(exc.response)) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Typefully request failed: {exc}") from exc

        if response.status_code == 204 or not response.content:
            return {"ok": True}
        return response.json()

    @staticmethod
    def _format_http_error(response: httpx.Response) -> str:
        detail = response.text
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")
                if code and message:
                    detail = f"{code}: {message}"
                elif message:
                    detail = str(message)
        return f"Typefully API error {response.status_code}: {detail}"

    def _resolve_social_set_id(self, social_set_id: int | None = None) -> int:
        if social_set_id is not None:
            return social_set_id

        configured = self._get_default_social_set_id()
        if configured is not None:
            return configured

        social_sets = self.list_social_sets(limit=2)
        results = social_sets.get("results", [])
        count = social_sets.get("count")
        if len(results) == 1 and (count in (None, 1)):
            return int(results[0]["id"])
        if not results:
            raise RuntimeError("No Typefully social sets are available for this API key.")

        choices = ", ".join(
            f"{item.get('id')} ({item.get('username') or item.get('name') or 'unnamed'})"
            for item in results
        )
        raise RuntimeError(
            "Multiple Typefully social sets are available. Pass social_set_id "
            f"explicitly or set TYPEFULLY_DEFAULT_SOCIAL_SET_ID. Candidates: {choices}"
        )

    @staticmethod
    def _validate_posts(posts: list[str]) -> None:
        if not posts:
            raise ValueError("posts must contain at least one post")
        if len(posts) > 50:
            raise ValueError("posts cannot contain more than 50 posts")
        if not all(isinstance(post, str) and post.strip() for post in posts):
            raise ValueError("posts must contain only non-empty strings")

    @staticmethod
    def _validate_platforms(platforms: dict[str, Any]) -> None:
        if not platforms:
            raise ValueError("platforms must include at least one platform")
        unknown = sorted(set(platforms) - ALLOWED_PLATFORMS)
        if unknown:
            raise ValueError(f"unsupported Typefully platform(s): {', '.join(unknown)}")

    def me(self) -> dict[str, Any]:
        """Get the authenticated Typefully user."""
        return self._request("GET", "/v2/me")

    def list_social_sets(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List social sets available to the authenticated Typefully user."""
        return self._request(
            "GET",
            "/v2/social-sets",
            params={"limit": limit, "offset": offset},
        )

    def get_social_set(self, social_set_id: int | None = None) -> dict[str, Any]:
        """Get social set details, including connected platforms and publishing quota."""
        resolved_id = self._resolve_social_set_id(social_set_id)
        return self._request("GET", f"/v2/social-sets/{resolved_id}/")

    def list_drafts(
        self,
        social_set_id: int | None = None,
        status: str | None = None,
        tag: list[str] | None = None,
        order_by: str = "-updated_at",
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List drafts with optional status, tag, ordering, and pagination filters."""
        if status is not None and status not in ALLOWED_DRAFT_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(ALLOWED_DRAFT_STATUSES)}, got {status!r}"
            )
        if order_by not in ALLOWED_DRAFT_ORDER_BY:
            raise ValueError(
                f"order_by must be one of {sorted(ALLOWED_DRAFT_ORDER_BY)}, got {order_by!r}"
            )
        resolved_id = self._resolve_social_set_id(social_set_id)
        return self._request(
            "GET",
            f"/v2/social-sets/{resolved_id}/drafts",
            params={
                "status": status,
                "tag": tag,
                "order_by": order_by,
                "limit": limit,
                "offset": offset,
            },
        )

    def get_draft(
        self,
        draft_id: int,
        social_set_id: int | None = None,
        exclude_comment_markers: bool = False,
    ) -> dict[str, Any]:
        """Get one draft. Keep comment markers by default so edits can preserve anchors."""
        resolved_id = self._resolve_social_set_id(social_set_id)
        return self._request(
            "GET",
            f"/v2/social-sets/{resolved_id}/drafts/{draft_id}",
            params={"exclude_comment_markers": exclude_comment_markers},
        )

    def create_x_draft(
        self,
        posts: list[str],
        social_set_id: int | None = None,
        draft_title: str | None = None,
        scratchpad_text: str | None = None,
        tags: list[str] | None = None,
        share: bool = False,
        publish_at: str | None = None,
        reply_to_url: str | None = None,
        quote_post_url: str | None = None,
        community_id: str | None = None,
        share_with_followers: bool | None = None,
        made_with_ai: bool = False,
        paid_partnership: bool = False,
    ) -> dict[str, Any]:
        """Create an X/Twitter draft or thread. Set publish_at='now' to publish immediately."""
        self._validate_posts(posts)
        x_posts: list[dict[str, Any]] = []
        for index, text in enumerate(posts):
            post: dict[str, Any] = {"text": text}
            if index == 0 and quote_post_url:
                post["quote_post_url"] = quote_post_url
            if made_with_ai:
                post["made_with_ai"] = True
            if paid_partnership:
                post["paid_partnership"] = True
            x_posts.append(post)

        settings = _remove_none(
            {
                "reply_to_url": reply_to_url,
                "community_id": community_id,
                "share_with_followers": share_with_followers,
            }
        )
        platform: dict[str, Any] = {"enabled": True, "posts": x_posts}
        if settings:
            platform["settings"] = settings

        return self.create_draft(
            platforms={"x": platform},
            social_set_id=social_set_id,
            draft_title=draft_title,
            scratchpad_text=scratchpad_text,
            tags=tags,
            share=share,
            publish_at=publish_at,
        )

    def create_draft(
        self,
        platforms: dict[str, Any],
        social_set_id: int | None = None,
        draft_title: str | None = None,
        scratchpad_text: str | None = None,
        tags: list[str] | None = None,
        share: bool = False,
        publish_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a Typefully draft for one or more platforms."""
        self._validate_platforms(platforms)
        resolved_id = self._resolve_social_set_id(social_set_id)
        return self._request(
            "POST",
            f"/v2/social-sets/{resolved_id}/drafts",
            json_body={
                "platforms": platforms,
                "draft_title": draft_title,
                "scratchpad_text": scratchpad_text,
                "tags": tags,
                "share": share,
                "publish_at": publish_at,
            },
        )

    def update_draft(
        self,
        draft_id: int,
        platforms: dict[str, Any] | None = None,
        social_set_id: int | None = None,
        draft_title: str | None = None,
        scratchpad_text: str | None = None,
        tags: list[str] | None = None,
        share: bool | None = None,
        publish_at: str | None = None,
        exclude_comment_markers: bool = False,
        force_overwrite_comments: bool = False,
    ) -> dict[str, Any]:
        """Update a draft. Set publish_at='now' to publish or an ISO datetime to schedule."""
        if platforms is not None:
            self._validate_platforms(platforms)

        body = _remove_none(
            {
                "platforms": platforms,
                "draft_title": draft_title,
                "scratchpad_text": scratchpad_text,
                "tags": tags,
                "share": share,
                "publish_at": publish_at,
                "force_overwrite_comments": force_overwrite_comments or None,
            }
        )
        if not body:
            raise ValueError("update_draft requires at least one field to update")

        resolved_id = self._resolve_social_set_id(social_set_id)
        return self._request(
            "PATCH",
            f"/v2/social-sets/{resolved_id}/drafts/{draft_id}",
            json_body=body,
            params={"exclude_comment_markers": exclude_comment_markers},
        )

    def schedule_draft(
        self,
        draft_id: int,
        publish_at: str,
        social_set_id: int | None = None,
    ) -> dict[str, Any]:
        """Schedule an existing draft at an ISO datetime or the next-free-slot queue slot."""
        return self.update_draft(
            draft_id=draft_id,
            social_set_id=social_set_id,
            publish_at=publish_at,
        )

    def publish_draft_now(
        self,
        draft_id: int,
        social_set_id: int | None = None,
    ) -> dict[str, Any]:
        """Publish an existing draft immediately."""
        return self.update_draft(
            draft_id=draft_id,
            social_set_id=social_set_id,
            publish_at="now",
        )

    def list_tags(self, social_set_id: int | None = None) -> dict[str, Any]:
        """List tags for a social set."""
        resolved_id = self._resolve_social_set_id(social_set_id)
        return self._request("GET", f"/v2/social-sets/{resolved_id}/tags")

    def create_tag(self, name: str, social_set_id: int | None = None) -> dict[str, Any]:
        """Create a tag in a social set."""
        resolved_id = self._resolve_social_set_id(social_set_id)
        return self._request(
            "POST",
            f"/v2/social-sets/{resolved_id}/tags",
            json_body={"name": name},
        )

    def get_queue(
        self,
        start_date: str,
        end_date: str,
        social_set_id: int | None = None,
    ) -> dict[str, Any]:
        """Get queue slots and scheduled drafts for a date range, using YYYY-MM-DD dates."""
        resolved_id = self._resolve_social_set_id(social_set_id)
        return self._request(
            "GET",
            f"/v2/social-sets/{resolved_id}/queue",
            params={"start_date": start_date, "end_date": end_date},
        )

    def get_queue_schedule(self, social_set_id: int | None = None) -> dict[str, Any]:
        """Get the queue schedule rules for a social set."""
        resolved_id = self._resolve_social_set_id(social_set_id)
        return self._request("GET", f"/v2/social-sets/{resolved_id}/queue/schedule")

    def resolve_linkedin_organization(
        self,
        organization_url: str,
        social_set_id: int | None = None,
    ) -> dict[str, Any]:
        """Resolve a LinkedIn company or school URL into Typefully mention syntax."""
        resolved_id = self._resolve_social_set_id(social_set_id)
        return self._request(
            "GET",
            f"/v2/social-sets/{resolved_id}/linkedin/organizations/resolve",
            params={"organization_url": organization_url},
        )

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> TypefullyClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _client() -> TypefullyClient:
    return TypefullyClient()
