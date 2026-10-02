"""The release helper never needs a package index or writable global cache."""
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


def test_build_uses_verified_assets_without_network_or_cache(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / 'scripts/build_release.py'
    output = tmp_path / 'wheels'
    calls = []
    monkeypatch.setattr(sys, 'argv', [str(script), '--output-dir', str(output)])
    monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: calls.append((command, kwargs)))
    module = runpy.run_path(str(script))
    module['main']()
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert all(flag in command for flag in (
        '--no-cache-dir', '--no-index', '--no-deps', '--no-build-isolation'))
    assert kwargs == {'check': True}
    with pytest.raises(SystemExit):
        module['main']()
    assert len(calls) == 1  # No build and no overwrite when output exists.
