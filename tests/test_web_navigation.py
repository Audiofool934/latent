"""Run the dependency-free browser navigation regressions when Node.js is available."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_web_navigation() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the web navigation regression tests")
    script = Path(__file__).with_suffix(".cjs")
    result = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
