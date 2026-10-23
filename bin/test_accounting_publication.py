"""Check that the exact accepted accounting sources import from this runtime root."""
import hashlib
from importlib import import_module
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    # Accepted cap-free accounting integration, merged as bffd267 (PR1).
    "capacity_budget": "4a3082377bf936c187be9c6b462106a28709154aa2301ce4072c9f79ebab094b",
    "capacity_guard": "8c8157550ec47fc1efc0be6ed2334cead3144e38e4d6b708e1ee65e4dd5024a1",
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
print(f"ACCOUNTING_PUBLICATION_CHECKS count={len(EXPECTED)}")
