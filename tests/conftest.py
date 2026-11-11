from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from short_video.runner import run_agent  # noqa: E402


@pytest.fixture
def run_case(tmp_path):
    def _run(example: str, *, faults=None, state_dir=None):
        return run_agent(
            input_path=ROOT / "examples" / example / "input.json",
            data_dir=ROOT / "data",
            state_dir=state_dir or (tmp_path / example),
            engine_name="offline",
            faults=faults or [],
        )

    return _run
