"""CLI for Mercury Banking API."""

from __future__ import annotations

import json
import sys
from typing import Any

import typer
from dotenv import load_dotenv
from rich.console import Console

from centaur_sdk import Table

from .client import MercuryClient

load_dotenv()

app = typer.Typer(name="mercury", help="Mercury Banking API CLI")
console = Console()


def _client() -> MercuryClient:
    return MercuryClient()


def _print(data: Any, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(data, indent=2, ensure_ascii=False), file=sys.stdout)
        return
    console.print(data)


def _collection(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _load_body(body: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter("body must decode to a JSON object")
    return payload


@app.command()
def accounts(json_output: bool = typer.Option(False, "--json", "-j", help="Output JSON")):
    """List Mercury accounts."""
    data = _client().get_accounts()
    if json_output:
        _print(data, json_output=True)
        return

    rows = _collection(data, "accounts", "data", "items")
    if not rows:
        console.print(data)
        return
    table = Table(title=f"Accounts ({len(rows)})")
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Name", style="cyan", max_width=30)
    table.add_column("Type", style="green", max_width=16)
    table.add_column("Balance", justify="right")
    for row in rows:
        balance = (
            row.get("availableBalance") or row.get("currentBalance") or row.get("balance") or ""
        )
        table.add_row(
            str(row.get("id", "")),
            str(row.get("name", "")),
            str(row.get("type", row.get("kind", ""))),
            str(balance),
        )
    console.print(table)


@app.command()
def transactions(
    account_id: str | None = typer.Option(None, "--account-id", help="Restrict to one account"),
    start: str | None = typer.Option(None, "--start", help="Start date"),
    end: str | None = typer.Option(None, "--end", help="End date"),
    status: str | None = typer.Option(None, "--status", help="Transaction status"),
    search: str | None = typer.Option(None, "--search", help="Search term"),
    limit: int | None = typer.Option(50, "--limit", "-n", help="Page limit"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
):
    """List transactions."""
    client = _client()
    if account_id:
        data = client.list_account_transactions(
            account_id, start=start, end=end, status=status, search=search, limit=limit
        )
    else:
        data = client.list_transactions(
            start=start, end=end, status=status, search=search, limit=limit
        )
    if json_output:
        _print(data, json_output=True)
        return

    rows = _collection(data, "transactions", "data", "items")
    if not rows:
        console.print(data)
        return
    table = Table(title=f"Transactions ({len(rows)})")
    table.add_column("Date", max_width=12)
    table.add_column("Description", style="cyan", max_width=42)
    table.add_column("Status", style="green", max_width=14)
    table.add_column("Amount", justify="right")
    for row in rows:
        table.add_row(
            str(row.get("postedAt") or row.get("createdAt") or row.get("date") or ""),
            str(row.get("counterpartyName") or row.get("description") or row.get("note") or ""),
            str(row.get("status", "")),
            str(row.get("amount") or row.get("amountInCents") or ""),
        )
    console.print(table)


@app.command()
def recipients(json_output: bool = typer.Option(False, "--json", "-j", help="Output JSON")):
    """List payment recipients."""
    data = _client().get_recipients()
    if json_output:
        _print(data, json_output=True)
        return

    rows = _collection(data, "recipients", "data", "items")
    if not rows:
        console.print(data)
        return
    table = Table(title=f"Recipients ({len(rows)})")
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Name", style="cyan", max_width=35)
    table.add_column("Status", style="green", max_width=16)
    for row in rows:
        table.add_row(str(row.get("id", "")), str(row.get("name", "")), str(row.get("status", "")))
    console.print(table)


@app.command("send-money")
def send_money(
    account_id: str = typer.Argument(..., help="Mercury account ID"),
    body: str = typer.Argument(..., help="JSON request body matching Mercury's API"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
):
    """Send money from an account to a recipient."""
    _print(_client().send_money(account_id, _load_body(body)), json_output=json_output)


@app.command("request-send-money")
def request_send_money(
    account_id: str = typer.Argument(..., help="Mercury account ID"),
    body: str = typer.Argument(..., help="JSON request body matching Mercury's API"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
):
    """Create a send-money approval request."""
    _print(_client().request_send_money(account_id, _load_body(body)), json_output=json_output)


@app.command("transfer")
def transfer(
    body: str = typer.Argument(..., help="JSON request body matching Mercury's API"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
):
    """Create an internal transfer."""
    _print(_client().create_internal_transfer(_load_body(body)), json_output=json_output)


@app.command("raw-request")
def raw_request(
    method: str = typer.Argument(..., help="HTTP method"),
    endpoint: str = typer.Argument(..., help="Relative API path"),
    body: str | None = typer.Option(None, "--body", help="JSON body"),
    json_output: bool = typer.Option(True, "--json/--no-json", help="Output JSON"),
):
    """Make a raw Mercury API request."""
    payload = _load_body(body) if body else None
    _print(_client().raw_request(method, endpoint, body=payload), json_output=json_output)


if __name__ == "__main__":
    app()
