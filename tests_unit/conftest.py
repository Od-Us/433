"""Load the Home Assistant independent modules without importing the integration.

``custom_components/rflink_ui/__init__.py`` imports Home Assistant. The protocol
and hub modules do not, so they are loaded here under a stand-in package name,
which lets these tests run without Home Assistant installed:

    python -m pytest tests_unit
"""

import asyncio
import importlib
import inspect
import sys
import types
from pathlib import Path

import pytest

PKG = "rflink_ui_core"
SRC = Path(__file__).resolve().parent.parent / "custom_components" / "rflink_ui"

if PKG not in sys.modules:
    package = types.ModuleType(PKG)
    package.__path__ = [str(SRC)]
    sys.modules[PKG] = package

protocol = importlib.import_module(f"{PKG}.protocol")
hub = importlib.import_module(f"{PKG}.hub")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run ``async def`` tests without requiring pytest-asyncio."""
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        args = {
            name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames
        }
        asyncio.run(asyncio.wait_for(pyfuncitem.obj(**args), 10))
        return True
    return None
