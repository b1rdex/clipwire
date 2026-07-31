"""Imports clipwire-agent.py under a legal module name."""
import importlib.util
import pathlib
import sys

_path = pathlib.Path(__file__).resolve().parent.parent / "clipwire-agent.py"
_spec = importlib.util.spec_from_file_location("clipwire_agent", _path)
_module = importlib.util.module_from_spec(_spec)
sys.modules["clipwire_agent"] = _module
_spec.loader.exec_module(_module)

globals().update({k: v for k, v in vars(_module).items() if not k.startswith("__")})
