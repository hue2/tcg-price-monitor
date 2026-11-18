import os
from pathlib import Path

import pytest
import yaml

from tcgwatch.config import parse_config

ROOT = Path(__file__).resolve().parents[1]

# Subprocesses spawned by tests (sys.executable -c ...) must import the repo under test, not whatever
# editable install a borrowed venv carries. Children inherit os.environ, so prepend <repo>/src once here.
_SRC = str(ROOT / "src")
_pp = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and p != _SRC]
os.environ["PYTHONPATH"] = os.pathsep.join([_SRC, *_pp])


@pytest.fixture
def example_cfg(tmp_path):
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    return parse_config(raw, base_dir=tmp_path)
