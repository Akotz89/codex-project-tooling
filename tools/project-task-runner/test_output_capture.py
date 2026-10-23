"""Complete, bounded-memory output capture for both generic runner drains."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNERS = (load(HERE / 'runner.py', 'output_capture_base'),
           load(HERE / 'adaptive/runner.py', 'output_capture_adaptive'))
PAYLOAD = bytes(range(256)) * 1024 + b'final-tail\x00\xff'


class FailedWriter:
    def __init__(self):
        self.calls = 0
        self.bytes_written = 0

    def write(self, data):
        self.calls += 1
        if self.calls > 1:
            raise OSError('injected log failure')
        self.bytes_written += len(data)
        return len(data)


class OutputCapture(unittest.TestCase):
    def counters(self):
        return {'bytes_seen': 0, 'bytes_kept': 0, 'truncated': False,
                'stream_eof': False, 'capture_error': None, 'stream_error': None,
                'stream_error_before_exit': False}

    def complete(self, runner, child, log, cleanup, *, patched_read=None):
        counters = self.counters()
        records = []

        def publish(returncode, first_failure):
            if records:
                raise FileExistsError('payload-exit is immutable')
            records.append({'returncode': returncode, 'first_failure': first_failure})

        complete_payload = runner.complete_payload if hasattr(runner, 'complete_payload') else runner.base.complete_payload
        patcher = None
        try:
            if patched_read is not None:
                patcher = patch.object(runner.os, 'read', side_effect=patched_read)
                patcher.start()
            result = complete_payload(
                child, log, counters, lambda: None, cleanup, publish,
                capture=runner.capture_output)
        finally:
            if patcher is not None:
                patcher.stop()
        return counters, records, result

    def test_both_caller_drain_loops_capture_binary_through_final_eof(self):
        for runner in RUNNERS:
            with self.subTest(runner=runner.__name__):
                source = 'import os; os.write(1, bytes(range(256))*1024 + b"final-tail\\x00\\xff")'
                child = subprocess.Popen([sys.executable, '-c', source], stdout=subprocess.PIPE)
                os.set_blocking(child.stdout.fileno(), False)
                log = io.BytesIO()
                counters, records, (code, first, cleanup) = self.complete(
                    runner, child, log, lambda on_tick, on_exit: (on_tick(), {'quiescent': True})[1])
                child.stdout.close()
                self.assertEqual(code, 0)
                self.assertIsNone(first)
                self.assertTrue(cleanup['quiescent'])
                self.assertEqual(len(records), 1)
                self.assertEqual(counters['bytes_seen'], len(PAYLOAD))
                self.assertEqual(counters['bytes_kept'], len(PAYLOAD))
                self.assertFalse(counters['truncated'])
                self.assertTrue(counters['stream_eof'])
                self.assertEqual(hashlib.sha256(log.getvalue()).hexdigest(),
                                 hashlib.sha256(PAYLOAD).hexdigest())

    def test_empty_nonblocking_pipe_is_not_eof_or_a_blocking_read(self):
        for runner in RUNNERS:
            with self.subTest(runner=runner.__name__):
                reader, writer = os.pipe()
                try:
                    pipe = os.fdopen(reader, 'rb', buffering=0, closefd=False)
                    os.set_blocking(pipe.fileno(), False)
                    counters = self.counters()
                    start = time.monotonic()
                    self.assertIsNone(runner.drain(pipe, io.BytesIO(), counters))
                    self.assertLess(time.monotonic() - start, .1)
                    self.assertFalse(counters['stream_eof'])
                    self.assertEqual(counters['bytes_seen'], 0)
                    os.write(writer, b'tail')
                    log = io.BytesIO()
                    self.assertTrue(runner.drain(pipe, log, counters))
                    os.close(writer)
                    writer = None
                    self.assertFalse(runner.drain(pipe, log, counters))
                    self.assertEqual(log.getvalue(), b'tail')
                    self.assertTrue(counters['stream_eof'])
                finally:
                    os.close(reader)
                    if writer is not None:
                        os.close(writer)

    def test_log_failure_is_reported_and_pipe_still_drains_to_eof(self):
        for runner in RUNNERS:
            with self.subTest(runner=runner.__name__):
                source = 'import os; os.write(1, bytes(range(256))*1024 + b"tail")'
                child = subprocess.Popen([sys.executable, '-c', source], stdout=subprocess.PIPE)
                os.set_blocking(child.stdout.fileno(), False)
                log = FailedWriter()
                counters, records, (code, first, cleanup) = self.complete(
                    runner, child, log, lambda on_tick, on_exit: (on_tick(), {'quiescent': True})[1])
                child.stdout.close()
                self.assertEqual(code, 0)
                self.assertEqual(first, {'phase': 'output_capture', 'code': 'OSError'})
                self.assertEqual(records[0]['returncode'], 0)
                self.assertIn(records[0]['first_failure'],
                              (None, {'phase': 'output_capture', 'code': 'OSError'}))
                self.assertTrue(cleanup['quiescent'])
                self.assertEqual(counters['bytes_seen'], len(bytes(range(256)) * 1024 + b'tail'))
                self.assertEqual(counters['bytes_kept'], log.bytes_written)
                self.assertEqual(counters['capture_error'], 'OSError')
                self.assertTrue(counters['truncated'])
                self.assertTrue(counters['stream_eof'])

    @staticmethod
    def running(pid):
        try:
            statline = (Path('/proc') / str(pid) / 'stat').read_text()
        except OSError:
            return False
        return statline.rsplit(')', 1)[1].split()[0] != 'Z'

    def test_owned_descendant_cleanup_runs_while_capturing_to_eof(self):
        for runner in RUNNERS:
            with self.subTest(runner=runner.__name__):
                pid_reader, pid_writer = os.pipe()
                script = ('import os, subprocess, sys; '
                          'p=subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], '
                          'stdout=sys.stdout, stderr=sys.stdout); '
                          f'os.write({pid_writer}, str(p.pid).encode())')
                child = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                                         pass_fds=(pid_writer,))
                os.close(pid_writer)
                descendant = int(os.read(pid_reader, 64))
                os.close(pid_reader)
                os.set_blocking(child.stdout.fileno(), False)
                cleanup_calls = []

                def cleanup(on_tick, on_child_exit):
                    cleanup_calls.append(descendant)
                    os.kill(descendant, signal.SIGTERM)
                    deadline = time.monotonic() + 2
                    while self.running(descendant) and time.monotonic() < deadline:
                        on_tick()
                        time.sleep(.01)
                    on_tick()
                    return {'quiescent': not self.running(descendant)}

                try:
                    counters, records, (code, first, result) = self.complete(
                        runner, child, io.BytesIO(), cleanup)
                    self.assertEqual(code, 0)
                    self.assertIsNone(first)
                    self.assertTrue(cleanup_calls)
                    self.assertFalse(self.running(descendant))
                    self.assertTrue(counters['stream_eof'])
                    self.assertIsNone(counters['stream_error'])
                    self.assertTrue(result['quiescent'])
                    self.assertEqual(len(records), 1)
                finally:
                    if self.running(descendant):
                        os.kill(descendant, signal.SIGKILL)
                    child.stdout.close()
                    child.wait()

    def test_payload_exit_is_published_before_owned_cleanup_starts(self):
        for runner in RUNNERS:
            with self.subTest(runner=runner.__name__):
                child = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(7)'],
                                         stdout=subprocess.PIPE)
                os.set_blocking(child.stdout.fileno(), False)
                events = []
                base = runner if hasattr(runner, 'complete_payload') else runner.base
                counters = self.counters()
                records = []

                def publish(returncode, failure):
                    events.append('payload_exit_published')
                    records.append({'returncode': returncode, 'first_failure': failure})

                def cleanup(on_tick, on_child_exit):
                    events.append('cleanup_started')
                    return {'quiescent': True}

                try:
                    result = base.complete_payload(child, io.BytesIO(), counters,
                                                   lambda: None, cleanup, publish,
                                                   capture=runner.capture_output)
                finally:
                    child.stdout.close()
                self.assertEqual(events, ['payload_exit_published', 'cleanup_started'])
                self.assertEqual(records, [{'returncode': 7,
                                            'first_failure': {'phase': 'payload', 'code': 'exit', 'returncode': 7}}])
                self.assertEqual(result[0], 7)

    def test_cleanup_helper_preserves_observed_real_child_status(self):
        runner = RUNNERS[0]
        child = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(7)'],
                                 stdout=subprocess.DEVNULL)
        status = None
        deadline = time.monotonic() + 2
        while (status is None or status.si_pid == 0) and time.monotonic() < deadline:
            status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if status is None or status.si_pid == 0:
                time.sleep(.005)
        self.assertIsNotNone(status, 'fixture child did not exit in time')
        self.assertNotEqual(status.si_pid, 0, 'fixture child did not exit in time')
        self.assertEqual(status.si_status, 7)
        observed = []

        def members(cgroup, recursive=False):
            return [(child.pid, cgroup)] if child.returncode is None else []

        try:
            with patch.object(runner, 'subtree_members', side_effect=members), \
                    patch.object(runner, 'signal_members'):
                cleanup = runner.cleanup_members(
                    '/disposable/test-cgroup', child,
                    on_child_exit=observed.append)
            self.assertEqual(observed, [7])
            self.assertEqual(child.returncode, 7)
            self.assertTrue(cleanup['quiescent'], cleanup)
        finally:
            if child.returncode is None:
                child.wait(timeout=2)

    def test_final_cleanup_one_arg_adapter_publishes_after_early_capture_error(self):
        for runner in RUNNERS:
            with self.subTest(runner=runner.__name__):
                base = runner if hasattr(runner, 'complete_payload') else runner.base
                child = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(7)'],
                                         stdout=subprocess.DEVNULL)
                deadline = time.monotonic() + 2
                status = None
                while (status is None or status.si_pid == 0) and time.monotonic() < deadline:
                    status = os.waitid(os.P_PID, child.pid,
                                       os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if status is None or status.si_pid == 0:
                        time.sleep(.005)
                self.assertIsNotNone(status)
                self.assertEqual(status.si_status, 7)
                counters = self.counters()
                counters['cleanup_error'] = 'OSError'
                caller = {'code': None, 'first': None}
                receipts = []

                def remember(code, failure):
                    caller.update(code=code, first=failure)

                def persist(code, failure):
                    if receipts:
                        raise FileExistsError('payload-exit is immutable')
                    receipts.append({'returncode': code, 'first_failure': failure})

                def worker_publisher(code, failure):
                    base.record_payload_exit(code, failure, remember, persist)

                def cleanup_callback(code):
                    base.publish_observed_payload_exit(code, caller['first'],
                                                       counters, worker_publisher)

                def members(cgroup, recursive=False):
                    return [(child.pid, cgroup)] if child.returncode is None else []

                try:
                    with patch.object(base, 'subtree_members', side_effect=members), \
                            patch.object(base, 'signal_members'):
                        cleanup = base.cleanup_members(
                            '/disposable/test-cgroup', child,
                            on_child_exit=cleanup_callback)
                    cleanup['capture_error'] = counters['cleanup_error']
                    result = base.result_record(
                        {'job_id': 'capture-test'}, 'intent-sha', 'invocation',
                        caller['code'], caller['first'], False, cleanup, False)
                    failure = {'phase': 'payload', 'code': 'exit', 'returncode': 7}
                    self.assertTrue(cleanup['quiescent'], cleanup)
                    self.assertEqual(receipts, [{'returncode': 7, 'first_failure': failure}])
                    self.assertEqual(caller, {'code': 7, 'first': failure})
                    self.assertEqual(result['payload_returncode'], 7)
                    self.assertEqual(result['first_failure'], failure)
                    self.assertEqual(result['cleanup']['capture_error'], 'OSError')
                finally:
                    if child.returncode is None:
                        child.wait(timeout=2)

    def test_nonzero_tail_read_fault_persists_payload_failure_once(self):
        for runner in RUNNERS:
            with self.subTest(runner=runner.__name__):
                child = subprocess.Popen([sys.executable, '-c', 'import os; os.write(1,b"tail"); raise SystemExit(7)'],
                                         stdout=subprocess.PIPE)
                os.set_blocking(child.stdout.fileno(), False)
                calls = 0
                fd = child.stdout.fileno()

                def read_fault(target, size):
                    nonlocal calls
                    if target != fd:
                        return os.read(target, size)
                    if calls == 0:
                        try:
                            chunk = original_read(target, size)
                        except BlockingIOError:
                            raise
                        if chunk:
                            calls += 1
                            return chunk
                    raise OSError('injected tail read fault')

                original_read = os.read

                def cleanup(on_tick, on_child_exit):
                    on_tick()
                    on_tick()
                    return {'quiescent': True}

                counters, records, (code, first, result) = self.complete(
                    runner, child, io.BytesIO(), cleanup, patched_read=read_fault)
                child.stdout.close()
                self.assertEqual(code, 7)
                self.assertEqual(first, {'phase': 'payload', 'code': 'exit', 'returncode': 7})
                self.assertEqual(records, [{'returncode': 7, 'first_failure': first}])
                self.assertEqual(counters['bytes_seen'], 4)
                self.assertFalse(counters['stream_eof'])

    def test_nonzero_exit_is_published_before_cleanup_error_propagates(self):
        for runner in RUNNERS:
            with self.subTest(runner=runner.__name__):
                child = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(7)'],
                                         stdout=subprocess.PIPE)
                os.set_blocking(child.stdout.fileno(), False)

                def cleanup(on_tick, on_child_exit):
                    raise OSError('injected cleanup failure')

                counters = self.counters()
                records = []
                caller_state = {'returncode': None, 'first_failure': None}

                def remember(returncode, first_failure):
                    caller_state['returncode'] = returncode
                    caller_state['first_failure'] = first_failure

                def persist(returncode, first_failure):
                    if records:
                        raise FileExistsError('payload-exit is immutable')
                    records.append({'returncode': returncode, 'first_failure': first_failure})

                base = runner if hasattr(runner, 'complete_payload') else runner.base

                def publish(returncode, first_failure):
                    base.record_payload_exit(returncode, first_failure, remember, persist)

                complete_payload = base.complete_payload
                with self.assertRaisesRegex(OSError, 'injected cleanup failure'):
                    complete_payload(child, io.BytesIO(), counters, lambda: None,
                                     cleanup, publish, capture=runner.capture_output)
                child.stdout.close()
                payload_failure = {'phase': 'payload', 'code': 'exit', 'returncode': 7}
                self.assertEqual(records, [{'returncode': 7, 'first_failure': payload_failure}])
                self.assertEqual(caller_state, {'returncode': 7, 'first_failure': payload_failure})
                self.assertEqual(counters['cleanup_error'], 'OSError')
                self.assertFalse(counters['stream_eof'])
                result = base.result_record(
                    {'job_id': 'capture-test'}, 'intent-sha', 'invocation',
                    caller_state['returncode'], caller_state['first_failure'], False,
                    {'quiescent': False, 'errors': ['OSError'],
                     'capture_error': counters['cleanup_error']}, False)
                self.assertEqual(result['payload_returncode'], 7)
                self.assertEqual(result['first_failure'], payload_failure)
                self.assertEqual(result['cleanup']['capture_error'], 'OSError')

    def test_live_read_error_cleans_up_without_waiting_for_live_payload(self):
        for runner in RUNNERS:
            with self.subTest(runner=runner.__name__):
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                         stdout=subprocess.PIPE)
                os.set_blocking(child.stdout.fileno(), False)
                fd = child.stdout.fileno()
                original_read = os.read

                def read_fault(target, size):
                    if target == fd:
                        raise OSError('injected live read fault')
                    return original_read(target, size)

                def cleanup(on_tick, on_child_exit):
                    try:
                        os.kill(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    on_tick()
                    child.wait(timeout=2)
                    return {'quiescent': child.poll() is not None}

                counters, records, (code, first, result) = self.complete(
                    runner, child, io.BytesIO(), cleanup, patched_read=read_fault)
                child.stdout.close()
                self.assertIsNotNone(code)
                self.assertTrue(counters['stream_error_before_exit'])
                self.assertEqual(first, {'phase': 'output_capture', 'code': 'OSError'})
                self.assertEqual(records, [{'returncode': code, 'first_failure': first}])
                self.assertTrue(result['quiescent'])


if __name__ == '__main__':
    unittest.main()
