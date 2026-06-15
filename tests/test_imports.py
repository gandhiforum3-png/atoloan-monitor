"""Import-integrity gate for the shared framework modules.

Asserts that every domain-agnostic module under agent/shared/ imports cleanly
and that NONE of them couple to the K8s API (kubernetes_asyncio). This is the
structural proof that the extraction of Claude-driving control flow (D-06..D-09)
into agent/shared/ stayed domain-free — a new domain reuses these modules without
dragging in kubernetes_asyncio.
"""

import glob
import importlib
import os

import pytest

# Project root = parent of the tests/ directory.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED_DIR = os.path.join(_ROOT, "agent", "shared")

_SHARED_MODULES = [
    "agent.shared.diagnoser_base",
    "agent.shared.agent_loop",
    "agent.shared.remediation",
    "agent.shared.models",
    "agent.shared.safety",
    "agent.shared.event_bus",
]


@pytest.mark.parametrize("module_name", _SHARED_MODULES)
def test_shared_module_imports(module_name):
    """Every shared module imports without error."""
    mod = importlib.import_module(module_name)
    assert mod is not None


def test_diagnoser_base_exposes_diagnose_with_claude():
    mod = importlib.import_module("agent.shared.diagnoser_base")
    assert hasattr(mod, "diagnose_with_claude")


def _shared_py_files():
    return sorted(glob.glob(os.path.join(_SHARED_DIR, "*.py")))


def test_shared_dir_has_python_files():
    """Sanity: the glob actually finds the shared modules it is gating."""
    files = _shared_py_files()
    assert files, "expected agent/shared/*.py files to exist"
    basenames = {os.path.basename(f) for f in files}
    assert "diagnoser_base.py" in basenames


@pytest.mark.parametrize("path", _shared_py_files())
def test_no_kubernetes_asyncio_in_shared(path):
    """No file under agent/shared/ may import or reference kubernetes_asyncio."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "kubernetes_asyncio" not in text, (
        f"{os.path.basename(path)} must be K8s-API-free but references kubernetes_asyncio"
    )
