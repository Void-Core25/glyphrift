"""Command-line entry point. Commands are added phase by phase (see ROADMAP.md)."""

import typer

from glyphrift import __version__

app = typer.Typer(help="GLYPHRIFT: code graph with evidence and confidence labels.")


@app.callback()
def main() -> None:
    """GLYPHRIFT command-line interface."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
