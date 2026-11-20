"""CLI entry point — Typer app for the headless relay-board MQTT device double."""

from __future__ import annotations

import typer

from .commands import relay

app = typer.Typer(
    name="scud",
    help="SCUD relay-board device emulator — headless MQTT device doubles.",
    no_args_is_help=True,
)

app.add_typer(relay.app)


if __name__ == "__main__":
    app()
