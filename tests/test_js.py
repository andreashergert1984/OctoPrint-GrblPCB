"""Run the JavaScript tests in tests/js with Node, if available."""
import pathlib
import shutil
import subprocess

import pytest

JS_TESTS = sorted((pathlib.Path(__file__).parent / "js").glob("*.test.js"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("script", JS_TESTS, ids=lambda p: p.name)
def test_javascript(script):
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
