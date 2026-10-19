#!/usr/bin/env python3
"""Identity of the exact project executables exercised by runtime review."""
import hashlib
from pathlib import Path


def identity(root):
    value = hashlib.sha256()
    for name in ("linear_intake.py", "acceptance.py", "candidate_identity.py"):
        path = Path(root) / "harness" / name
        if not path.is_file() or path.is_symlink() or not path.stat().st_size:
            raise ValueError(f"missing or unsafe runtime executable: {name}")
        value.update(name.encode() + b"\0")
        with path.open("rb") as stream:
            value.update(hashlib.file_digest(stream, "sha256").digest())
    return value.hexdigest()


if __name__ == "__main__":
    print(identity(Path(__file__).resolve().parent.parent))
