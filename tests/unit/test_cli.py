import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

from glyphrift import __version__  # noqa: E402
from glyphrift.cli import app  # noqa: E402

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
