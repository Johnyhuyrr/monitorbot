"""The two original offline suites, run as they always were, so `pytest`
alone covers everything."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from conftest import ROOT


@pytest.mark.parametrize("script", ["selftest.py", "test_auto_monitor.py"])
def test_original_suite_passes(script, tmp_path):
    out = subprocess.run([sys.executable, str(ROOT / script)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=600,
                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert out.returncode == 0, out.stdout[-4000:] + out.stderr[-4000:]
    assert "All checks passed." in out.stdout
