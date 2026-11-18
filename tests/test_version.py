"""The package version must match pyproject.toml (SPEC status is v0.8)."""

import tomllib
from pathlib import Path

import tcgwatch

ROOT = Path(__file__).resolve().parent.parent


def test_version_matches_pyproject():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert tcgwatch.__version__ == data["project"]["version"] == "0.12.0"
