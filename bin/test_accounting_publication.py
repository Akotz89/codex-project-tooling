"""Check that the exact accepted accounting sources import from this runtime root."""
import hashlib
from importlib import import_module
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "capacity_budget": "5704525cb4cf73d0cac9444197fa1e80d81ec93a917f9d474a2a7716b2ad96af",
    "capacity_guard": "076d152590a09b7db755bb5aa88e232b60354cfd8cb594369d6763726e930e72",
}

sys.path.insert(0, str(ROOT))
for name, digest in EXPECTED.items():
    path = ROOT / "accounting" / f"{name}.py"
    if not path.is_file():
        raise SystemExit(f"missing required accounting module: {path}")
    module = import_module(f"accounting.{name}")
    if Path(module.__file__).resolve() != path.resolve():
        raise SystemExit(f"accounting.{name} imported from unexpected path: {module.__file__}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != digest:
        raise SystemExit(f"accounting.{name} source hash mismatch: {actual}")
print("accounting modules import from the runtime root with accepted source hashes")
