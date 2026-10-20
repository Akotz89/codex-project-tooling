"""Development adapter; installed adapters carry an exact source and project binding."""
import importlib.util as _util
from pathlib import Path as _Path
import sys as _sys
_sys.dont_write_bytecode = True
_source = _Path(__file__).resolve().parents[2]
_loader = _source / "runtime/adapter.py"
_name = "_factory_adapter_" + __import__("hashlib").sha256(str(_source).encode()).hexdigest()
if _name not in _sys.modules:
    _spec = _util.spec_from_file_location(_name, _loader)
    _module = _util.module_from_spec(_spec)
    _sys.modules[_name] = _module
    _spec.loader.exec_module(_module)
_sys.modules[_name].development(globals(), 'factory/consumer.py', _source)
