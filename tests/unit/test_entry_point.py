import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(sys.executable).parent


@pytest.mark.parametrize(
    "cmd",
    [[str(BIN / "brainrec"), "--help"], [sys.executable, "-m", "brainrec", "--help"]],
    ids=["console-script", "python-m"],
)
def test_entry_point_starts(cmd):
    r = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "usage: brainrec" in r.stdout
