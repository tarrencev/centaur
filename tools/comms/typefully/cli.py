"""CLI for the Typefully tool."""

# ruff: noqa: B008, E402

from dotenv import load_dotenv

load_dotenv()

import json
from typing import Any

import typer
from rich.console import Console

from centaur_sdk.backends import EnvBackend, configure

from .client import _client

configure(EnvBackend())

app = typer.Typer(name="typefully", help="Typefully drafts, scheduling, publishing, and queue")
console = Console()


def _print(data: Any, json_output: bool) -> None:
    if json_output:
        print(json.dumps(data, indent=2))
    else:
        console.print_json(data=data)


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter("value must be a JSON object")
    return parsed


@app.command()
def me(json_output: bool = typer.Option(False, "--json", help="Output JSON")):
    """Get the authenticated Typefully user."""
    _print(_client().me(), json_output)


@app.command("social-sets")
def social_sets(
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """List available social sets."""
    _print(_client().list_social_sets(limit=limit, offset=offset), json_output)


@app.command("social-set")
def social_set(
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """Get details for one social set."""
    _print(_client().get_social_set(social_set_id=social_set_id), json_output)


@app.command("drafts")
def drafts(
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    status: str | None = typer.Option(None, "--status"),
    tag: str | None = typer.Option(None, "--tag", help="Comma-separated tag slugs"),
    order_by: str = typer.Option("-updated_at", "--order-by"),
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=50),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """List drafts."""
    data = _client().list_drafts(
        social_set_id=social_set_id,
        status=status,
        tag=_split_csv(tag),
        order_by=order_by,
        limit=limit,
        offset=offset,
    )
    _print(data, json_output)


@app.command("draft")
def draft(
    draft_id: int = typer.Argument(..., help="Typefully draft ID"),
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    exclude_comment_markers: bool = typer.Option(False, "--exclude-comment-markers"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """Get a draft."""
    _print(
        _client().get_draft(
            draft_id,
            social_set_id=social_set_id,
            exclude_comment_markers=exclude_comment_markers,
        ),
        json_output,
    )


@app.command("create-x")
def create_x(
    text: list[str] | None = typer.Option(None, "--text", "-t", help="Post text; repeat for a thread"),
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    draft_title: str | None = typer.Option(None, "--title"),
    scratchpad_text: str | None = typer.Option(None, "--scratchpad"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tag slugs"),
    share: bool = typer.Option(False, "--share"),
    publish_at: str | None = typer.Option(None, "--publish-at"),
    reply_to_url: str | None = typer.Option(None, "--reply-to-url"),
    quote_post_url: str | None = typer.Option(None, "--quote-post-url"),
    community_id: str | None = typer.Option(None, "--community-id"),
    share_with_followers: bool | None = typer.Option(None, "--share-with-followers/--no-share-with-followers"),
    made_with_ai: bool = typer.Option(False, "--made-with-ai"),
    paid_partnership: bool = typer.Option(False, "--paid-partnership"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """Create an X draft/thread. Use --publish-at now to publish immediately."""
    if not text:
        raise typer.BadParameter("pass at least one --text value")
    data = _client().create_x_draft(
        posts=text,
        social_set_id=social_set_id,
        draft_title=draft_title,
        scratchpad_text=scratchpad_text,
        tags=_split_csv(tags),
        share=share,
        publish_at=publish_at,
        reply_to_url=reply_to_url,
        quote_post_url=quote_post_url,
        community_id=community_id,
        share_with_followers=share_with_followers,
        made_with_ai=made_with_ai,
        paid_partnership=paid_partnership,
    )
    _print(data, json_output)


@app.command("create")
def create(
    platforms_json: str = typer.Argument(..., help="Typefully platforms JSON object"),
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    draft_title: str | None = typer.Option(None, "--title"),
    scratchpad_text: str | None = typer.Option(None, "--scratchpad"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tag slugs"),
    share: bool = typer.Option(False, "--share"),
    publish_at: str | None = typer.Option(None, "--publish-at"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """Create a raw multi-platform draft from a Typefully platforms JSON object."""
    data = _client().create_draft(
        platforms=_parse_json_object(platforms_json),
        social_set_id=social_set_id,
        draft_title=draft_title,
        scratchpad_text=scratchpad_text,
        tags=_split_csv(tags),
        share=share,
        publish_at=publish_at,
    )
    _print(data, json_output)


@app.command("update")
def update(
    draft_id: int = typer.Argument(..., help="Typefully draft ID"),
    platforms_json: str | None = typer.Option(None, "--platforms-json"),
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    draft_title: str | None = typer.Option(None, "--title"),
    scratchpad_text: str | None = typer.Option(None, "--scratchpad"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tag slugs"),
    share: bool | None = typer.Option(None, "--share/--no-share"),
    publish_at: str | None = typer.Option(None, "--publish-at"),
    exclude_comment_markers: bool = typer.Option(False, "--exclude-comment-markers"),
    force_overwrite_comments: bool = typer.Option(False, "--force-overwrite-comments"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """Update a draft."""
    data = _client().update_draft(
        draft_id=draft_id,
        platforms=_parse_json_object(platforms_json) if platforms_json else None,
        social_set_id=social_set_id,
        draft_title=draft_title,
        scratchpad_text=scratchpad_text,
        tags=_split_csv(tags),
        share=share,
        publish_at=publish_at,
        exclude_comment_markers=exclude_comment_markers,
        force_overwrite_comments=force_overwrite_comments,
    )
    _print(data, json_output)


@app.command("schedule")
def schedule(
    draft_id: int = typer.Argument(..., help="Typefully draft ID"),
    publish_at: str = typer.Argument(..., help="ISO datetime or next-free-slot"),
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """Schedule a draft."""
    _print(
        _client().schedule_draft(draft_id, publish_at, social_set_id=social_set_id),
        json_output,
    )


@app.command("publish-now")
def publish_now(
    draft_id: int = typer.Argument(..., help="Typefully draft ID"),
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """Publish a draft immediately."""
    _print(_client().publish_draft_now(draft_id, social_set_id=social_set_id), json_output)


@app.command("tags")
def tags(
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """List tags."""
    _print(_client().list_tags(social_set_id=social_set_id), json_output)


@app.command("create-tag")
def create_tag(
    name: str = typer.Argument(..., help="Tag display name"),
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """Create a tag."""
    _print(_client().create_tag(name, social_set_id=social_set_id), json_output)


@app.command("queue")
def queue(
    start_date: str = typer.Argument(..., help="Start date, YYYY-MM-DD"),
    end_date: str = typer.Argument(..., help="End date, YYYY-MM-DD"),
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """Get queue items for a date range."""
    _print(_client().get_queue(start_date, end_date, social_set_id=social_set_id), json_output)


@app.command("queue-schedule")
def queue_schedule(
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """Get queue schedule rules."""
    _print(_client().get_queue_schedule(social_set_id=social_set_id), json_output)


@app.command("resolve-linkedin-org")
def resolve_linkedin_org(
    organization_url: str = typer.Argument(..., help="LinkedIn organization URL"),
    social_set_id: int | None = typer.Option(None, "--social-set-id"),
    json_output: bool = typer.Option(False, "--json", help="Output JSON"),
):
    """Resolve a LinkedIn organization URL into mention syntax."""
    _print(
        _client().resolve_linkedin_organization(
            organization_url,
            social_set_id=social_set_id,
        ),
        json_output,
    )


if __name__ == "__main__":
    app()
