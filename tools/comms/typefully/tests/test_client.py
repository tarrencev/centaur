from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[4]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "typefully_client", Path(__file__).parents[1] / "client.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

TypefullyClient = module.TypefullyClient


class RecordingTypefullyClient(TypefullyClient):
    def __init__(
        self,
        *,
        default_social_set_id: int | str | None = 123,
        responses: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            api_key="test-key",
            default_social_set_id=default_social_set_id,
            base_url="https://api.typefully.test",
        )
        self.calls: list[dict[str, Any]] = []
        self.responses = responses or {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        call = {
            "method": method,
            "path": path,
            "json_body": module._remove_none(json_body) if json_body is not None else None,
            "params": module._remove_none(params) if params is not None else None,
        }
        self.calls.append(call)
        return self.responses.get((method, path), {"ok": True, "id": 42})

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]


def test_create_x_draft_builds_thread_payload() -> None:
    client = RecordingTypefullyClient(default_social_set_id=99)

    client.create_x_draft(
        posts=["First post", "Second post"],
        draft_title="Launch thread",
        tags=["launch"],
        share=True,
        publish_at="next-free-slot",
        reply_to_url="https://x.com/typefully/status/1",
        quote_post_url="https://x.com/typefully/status/2",
        community_id="123",
        share_with_followers=False,
        made_with_ai=True,
        paid_partnership=True,
    )

    assert client.last["method"] == "POST"
    assert client.last["path"] == "/v2/social-sets/99/drafts"
    assert client.last["json_body"] == {
        "platforms": {
            "x": {
                "enabled": True,
                "posts": [
                    {
                        "text": "First post",
                        "quote_post_url": "https://x.com/typefully/status/2",
                        "made_with_ai": True,
                        "paid_partnership": True,
                    },
                    {
                        "text": "Second post",
                        "made_with_ai": True,
                        "paid_partnership": True,
                    },
                ],
                "settings": {
                    "reply_to_url": "https://x.com/typefully/status/1",
                    "community_id": "123",
                    "share_with_followers": False,
                },
            }
        },
        "draft_title": "Launch thread",
        "tags": ["launch"],
        "share": True,
        "publish_at": "next-free-slot",
    }


def test_create_draft_accepts_raw_multi_platform_payload() -> None:
    client = RecordingTypefullyClient()
    platforms = {
        "x": {"enabled": True, "posts": [{"text": "Short version"}]},
        "linkedin": {"enabled": True, "posts": [{"text": "Longer version"}]},
    }

    client.create_draft(platforms=platforms, social_set_id=555, scratchpad_text="notes")

    assert client.last["method"] == "POST"
    assert client.last["path"] == "/v2/social-sets/555/drafts"
    assert client.last["json_body"] == {
        "platforms": platforms,
        "scratchpad_text": "notes",
        "share": False,
    }


def test_update_schedule_and_publish_now_use_patch_publish_at() -> None:
    client = RecordingTypefullyClient(default_social_set_id=7)

    client.schedule_draft(10, "2026-06-01T14:00:00Z")
    assert client.last["method"] == "PATCH"
    assert client.last["path"] == "/v2/social-sets/7/drafts/10"
    assert client.last["json_body"] == {"publish_at": "2026-06-01T14:00:00Z"}

    client.publish_draft_now(10)
    assert client.last["method"] == "PATCH"
    assert client.last["path"] == "/v2/social-sets/7/drafts/10"
    assert client.last["json_body"] == {"publish_at": "now"}


def test_list_drafts_serializes_filters() -> None:
    client = RecordingTypefullyClient(default_social_set_id=3)

    client.list_drafts(status="scheduled", tag=["launch", "product"], limit=5, offset=10)

    assert client.last["method"] == "GET"
    assert client.last["path"] == "/v2/social-sets/3/drafts"
    assert client.last["params"] == {
        "status": "scheduled",
        "tag": ["launch", "product"],
        "order_by": "-updated_at",
        "limit": 5,
        "offset": 10,
    }


def test_social_set_resolution_uses_single_available_social_set() -> None:
    client = RecordingTypefullyClient(
        default_social_set_id="",
        responses={
            ("GET", "/v2/social-sets"): {
                "results": [{"id": 456, "username": "typefully"}],
                "count": 1,
            }
        },
    )

    client.get_social_set()

    assert client.calls[0]["path"] == "/v2/social-sets"
    assert client.calls[1]["path"] == "/v2/social-sets/456/"


def test_social_set_resolution_ignores_unresolved_default_stub() -> None:
    client = RecordingTypefullyClient(
        default_social_set_id="TYPEFULLY_DEFAULT_SOCIAL_SET_ID",
        responses={
            ("GET", "/v2/social-sets"): {
                "results": [{"id": 789, "username": "typefully"}],
                "count": 1,
            }
        },
    )

    client.get_social_set()

    assert client.calls[1]["path"] == "/v2/social-sets/789/"


def test_social_set_resolution_requires_choice_when_multiple_exist() -> None:
    client = RecordingTypefullyClient(
        default_social_set_id="",
        responses={
            ("GET", "/v2/social-sets"): {
                "results": [
                    {"id": 1, "username": "alpha"},
                    {"id": 2, "username": "beta"},
                ],
                "count": 2,
            }
        },
    )

    with pytest.raises(RuntimeError, match="Multiple Typefully social sets"):
        client.get_social_set()


def test_validation_rejects_empty_posts_and_unknown_platforms() -> None:
    client = RecordingTypefullyClient()

    with pytest.raises(ValueError, match="at least one post"):
        client.create_x_draft(posts=[])

    with pytest.raises(ValueError, match="unsupported Typefully platform"):
        client.create_draft(platforms={"instagram": {"enabled": True}}, social_set_id=1)
