"""Shared pytest configuration for pn-docgen tests."""

from __future__ import annotations

import sys
from pathlib import Path
import socket

import pytest


def pytest_addoption(parser):
    parser.addoption("--private-root", default=None,
                     help="Optional checkout containing private historical fixtures (never publish these)")
    parser.addoption('--private-baselines',default=None,
                     help='Private versioned acceptance directory with manifest.json (not for publication)')
    parser.addoption('--legacy-goldens',action='store_true',default=False,
                     help='Run historical 007/009/014 geometry comparisons; diagnostic, not current acceptance')


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker('legacy_golden') and not config.getoption('--legacy-goldens'):
            item.add_marker(pytest.mark.skip(reason='Historical layout contract: opt in with --legacy-goldens'))
        if item.get_closest_marker("private") and not config.getoption("--private-root"):
            item.add_marker(pytest.mark.skip(reason="Private replay: opt in with --private-root"))


@pytest.fixture
def private_root(request):
    path = Path(request.config.getoption("--private-root")).resolve()
    if not path.is_dir():
        pytest.fail(f"Private fixture root does not exist: {path}")
    return path


@pytest.fixture(autouse=True)
def private_paths(request, monkeypatch):
    if request.node.get_closest_marker("private"):
        root = request.getfixturevalue("private_root")
        if hasattr(request.module, "ROOT"):
            monkeypatch.setattr(request.module, "ROOT", root)
        if hasattr(request.module, "BASELINE"):
            monkeypatch.setattr(request.module, "BASELINE", root / "experiments/007-symmetry/out/baseline")


# Ensure local package imports work without editable install.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def offline_and_isolated_config(monkeypatch):
    """Tests use local fixtures and explicit fake AWS clients only."""
    from pndocgen.engine.core import config
    import boto3

    def forbidden(*args, **kwargs):
        raise AssertionError("Network/AWS access is forbidden in pn-docgen tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(boto3, "Session", forbidden)
    monkeypatch.setattr(boto3.session.Session, "client", forbidden)
    monkeypatch.setattr(boto3.session.Session, "resource", forbidden)
    monkeypatch.setattr(config, "_cached_config", config.AppConfig.from_yaml(ROOT / "pndocgen.yaml"))
