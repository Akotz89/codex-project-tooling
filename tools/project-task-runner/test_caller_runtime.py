import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import runner


SPEC = importlib.util.spec_from_file_location(
    "project_caller_runtime_test", Path(__file__).with_name("caller_runtime.py"))
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)
runtime.configure(runner, runner.need)


class CallerRuntimeTests(unittest.TestCase):
    def test_session_guard_checks_exact_worker_and_payload_birth(self):
        worker_pid = 10
        worker = {"pid": worker_pid, "start": 1}
        payload = {"pid": os.getpid(), "start": 2}
        identities = {worker_pid: worker, os.getpid(): payload}
        guard = runtime.SessionGuard(worker, payload, os.getppid(),
                                    process=identities.__getitem__, refuse=runner.need)
        guard.verify()
        identities[worker_pid] = {"pid": worker_pid, "start": 3}
        with self.assertRaisesRegex(runner.Refused, "live_payload_custody"):
            guard.verify()

    def test_control_reads_private_current_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.json"
            path.write_text(json.dumps({"version": 2, "job_id": "fixture", "cpu": 2,
                                        "fanout": 3, "checkpoint": False}))
            path.chmod(0o600)
            control = runtime.Control(path, "fixture", None, 4, cpu_ceiling=4)
            self.assertEqual(control.slots(), 3)

    def test_promotion_copies_and_pins_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir(mode=0o700)
            (source / "payload.json").write_bytes(b'{"ok":true}\n')
            promotion = runtime.Promotion(source, root / "promoted", None, None,
                                          owner_root=root)
            row = promotion.copy("payload.json")
            promotion.finalize(True)
            self.assertEqual(row["sha256"], runner.digest(b'{"ok":true}\n'))
            self.assertEqual((root / "promoted/payload.json").read_bytes(), b'{"ok":true}\n')
            with self.assertRaises(FileExistsError):
                promotion.copy("payload.json")

    def test_anchored_child_waits_and_reaps_same_leader(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        child = runtime.AnchoredChild(proc)
        self.assertEqual(child.wait(5), 0)
        self.assertTrue(child.reaped)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()

    def test_capture_drains_both_streams_to_owned_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = subprocess.Popen(
                [sys.executable, "-c", "import sys;print('out');print('err',file=sys.stderr)"],
                start_new_session=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            child = runtime.AnchoredChild(proc)
            result = runtime.capture(child, logs={"stdout": root / "stdout.log",
                                                   "stderr": root / "stderr.log"},
                                     project_root=root, owned_root=root)
            self.assertEqual(result.returncode, 0)
            self.assertEqual((root / "stdout.log").read_text(), "out\n")
            self.assertEqual((root / "stderr.log").read_text(), "err\n")
            self.assertEqual(result.capture["eof"], {"stdout": True, "stderr": True})


if __name__ == "__main__":
    unittest.main()
