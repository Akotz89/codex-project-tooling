from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = PROJECT_ROOT
TASK_WORK = ROOT / "work" / "factory-tests"


def setUpModule():
    TASK_WORK.mkdir(parents=True, exist_ok=True)


sys.path.insert(0, str(ROOT / "factory"))
import consumer  # noqa: E402
import jev_prepared_bridge as bridge  # noqa: E402
import jev_router  # noqa: E402


class DispatchArgsTests(unittest.TestCase):
    def test_generated_config_is_translated_and_literal_tail_is_untouched(self):
        args, requested, generated = jev_router.strip_opt_in([
            "archon-implement", "--jev-route", "--jev-generated-config", "work/run/config.json",
            "--no-worktree", "--", "--config", "literal worker text",
        ])
        self.assertTrue(requested)
        self.assertEqual(generated, "work/run/config.json")
        self.assertEqual(args, [
            "archon-implement", "--config", generated, "--no-worktree",
            "--", "--config", "literal worker text",
        ])
        resolved_args, resolved = jev_router.resolve_generated_config(args, generated, ROOT)
        self.assertEqual(resolved, str((ROOT / generated).resolve()))
        self.assertEqual(resolved_args[2], resolved)
        self.assertFalse(jev_router.explicit_choice(resolved_args, resolved))

    def test_human_config_and_explicit_model_choice_remain_explicit(self):
        self.assertTrue(jev_router.explicit_choice(["archon-implement", "--config", "human.json"]))
        self.assertTrue(jev_router.explicit_choice(
            ["archon-implement", "--config", "/run/generated.json", "--model", "gpt-6-luna"],
            "/run/generated.json",
        ))
        self.assertTrue(jev_router.explicit_choice([
            "archon-implement", "--config", "human.json", "--", "--model", "literal"
        ]))

    def test_generated_config_option_requires_unambiguous_opt_in(self):
        invalid = [
            ["archon-implement", "--jev-generated-config", "config.json"],
            ["archon-implement", "--jev-route", "--jev-generated-config"],
            ["archon-implement", "--jev-route", "--jev-generated-config", "a.json",
             "--jev-generated-config=b.json"],
            ["archon-implement", "--jev-route", "--config", "human.json",
             "--jev-generated-config", "generated.json"],
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                jev_router.strip_opt_in(args)
        # The option after `--` is user message text, not wrapper syntax.
        args, requested, generated = jev_router.strip_opt_in([
            "archon-implement", "--jev-route", "--", "--jev-generated-config", "literal"
        ])
        self.assertTrue(requested)
        self.assertIsNone(generated)
        self.assertEqual(args[-2:], ["--jev-generated-config", "literal"])

    def test_configured_bun_directory_is_added_once_to_runtime_path(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-path-", dir=TASK_WORK) as scratch:
            bindir = Path(scratch) / "bin"
            bindir.mkdir()
            bun = bindir / "bun"
            bun.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            bun.chmod(0o755)
            original = {"PATH": "/usr/bin:/bin", "KEEP": "yes"}
            result = consumer.with_runtime_path(original, {"bun": str(bun)})
            self.assertEqual(result["PATH"].split(os.pathsep).count(str(bindir)), 1)
            self.assertEqual(result["PATH"].split(os.pathsep)[0], str(bindir))
            self.assertEqual(original["PATH"], "/usr/bin:/bin")
            self.assertEqual(result["KEEP"], "yes")
            self.assertEqual(consumer.with_runtime_path(result, {"bun": str(bun)}), result)

    def test_configured_bun_precedes_ambient_even_when_already_later_in_path(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-path-order-", dir=TASK_WORK) as scratch:
            root = Path(scratch)
            ambient, configured = root / "ambient", root / "configured"
            ambient.mkdir(); configured.mkdir()
            for directory in (ambient, configured):
                executable = directory / "bun"
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)
            alias = root / "configured-alias"
            alias.symlink_to(configured, target_is_directory=True)
            path = os.pathsep.join([str(ambient), str(configured), str(configured / ".." / "configured"), str(alias), "/bin"])
            original = {"PATH": path, "KEEP": "yes"}
            result = consumer.with_runtime_path(original, {"bun": str(configured / "bun")})
            self.assertEqual(result["PATH"].split(os.pathsep), [str(configured), str(ambient), "/bin"])
            self.assertEqual(consumer.shutil.which("bun", path=result["PATH"]), str(configured / "bun"))
            self.assertEqual(consumer.with_runtime_path(result, {"bun": str(configured / "bun")}), result)
            self.assertEqual(original["PATH"], path)
            self.assertEqual(result["KEEP"], "yes")

    def test_invoke_preserves_config_and_bun_without_optional_preflight_authority(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-invoke-", dir=TASK_WORK) as scratch:
            root = Path(scratch)
            source = root / "source"
            (source / "packages/cli/src/commands").mkdir(parents=True)
            (source / "packages/cli/src/commands/jev-preflight.ts").write_text("", encoding="utf-8")
            config = root / "config.json"
            config.write_text("{}", encoding="utf-8")
            bindir = root / "runtime-bin"
            bindir.mkdir()
            bun = bindir / "bun"
            bun.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            bun.chmod(0o755)
            settings = {"bun": str(bun), "jev_router": {}}
            captured = []

            def execute(argv, cwd, **kwargs):
                captured.append((argv, kwargs.get("env", {})))
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.dict(os.environ, {"PATH": "/usr/bin", "ARCHON_JEV_GENERATED_CONFIG": "stale"}, clear=True), \
                    patch.object(consumer, "shared_root", return_value=root), \
                    patch.object(consumer, "read_settings", return_value=settings), \
                    patch.object(consumer, "verify_source", return_value=source), \
                    patch.object(consumer, "discover", return_value={"archon-implement": source}), \
                    patch.object(consumer, "validate"), \
                    patch.object(consumer, "execute", side_effect=execute):
                consumer.invoke(root, "run", [
                    "archon-implement", "--jev-route", "--jev-generated-config", str(config), "--", "task"
                ])
            argv, env = captured[-1]
            self.assertIn("--config", argv)
            self.assertIn(str(config), argv)
            self.assertNotIn("ARCHON_JEV_GENERATED_CONFIG", env)
            self.assertIn(str(bindir), env["PATH"].split(os.pathsep))
            self.assertNotIn("ARCHON_JEV_BRIDGE", env)

            captured.clear()
            with patch.dict(os.environ, {"PATH": "/usr/bin", "ARCHON_JEV_GENERATED_CONFIG": "stale"}, clear=True), \
                    patch.object(consumer, "shared_root", return_value=root), \
                    patch.object(consumer, "read_settings", return_value=settings), \
                    patch.object(consumer, "verify_source", return_value=source), \
                    patch.object(consumer, "discover", return_value={"archon-implement": source}), \
                    patch.object(consumer, "validate"), \
                    patch.object(consumer, "execute", side_effect=execute):
                consumer.invoke(root, "run", [
                    "archon-implement", "--jev-route", "--config", str(config), "--", "task"
                ])
            _, env = captured[-1]
            self.assertNotIn("ARCHON_JEV_BRIDGE", env)
            self.assertNotIn("ARCHON_JEV_GENERATED_CONFIG", env)


class TypedAssessmentBridgeTests(unittest.TestCase):
    def capability(self):
        return {'source': 'current_runtime_tool_schema', 'tool': 'collaboration.spawn_agent',
                'models': [{'model': model, 'reasoning_efforts': sorted(efforts)}
                           for model, efforts in jev_router.WORKER_EFFORTS.items()]}

    def request(self):
        return {'task': 'Bounded parser repair with known cause; validate caller and parser assertions in isolated source.',
                'ordinary': {'provider': 'codex', 'model': 'gpt-6-luna', 'effort': 'low'},
                'capability_provenance': self.capability(), 'remaining_ms': 4000}

    def answer(self, payload, model='gpt-6-luna', effort='medium', outcome=None):
        import hashlib
        options = payload['questions']['worker_pair']['criteria']
        choice = outcome or 'pair_' + hashlib.sha256(f'{model}\0{effort}'.encode()).hexdigest()[:16]
        return {'answers': {'worker_pair': {'type': 'choice', 'choice': choice, 'confidence': 1,
                'probabilities': {key: float(key == choice) for key in options}}}}

    def test_actual_typed_pair_reaches_prepared_arguments(self):
        with patch.object(bridge, 'read_credential', return_value='offline-test-token'):
            seen = []
            result = bridge.route(self.request(), ROOT,
                transport=lambda payload, *_: seen.append(payload) or self.answer(payload, effort='xhigh'))
        self.assertEqual(result['status'], 'selected')
        self.assertEqual(result['spawn_agent'], {'model': 'gpt-6-luna', 'reasoning_effort': 'xhigh', 'fork_turns': 'none'})
        self.assertEqual(result['receipt']['schema'], 'CPI236TASKASSESSMENTV1')
        self.assertEqual(result['receipt']['status'], 'choice_proposed')
        self.assertEqual(result['dispatch_receipt']['dispatch'], 'not_invoked')
        self.assertEqual(result['receipt']['attempts'], 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual({(r['model'], r['effort']) for r in seen[0]['state']['allowed_catalog']},
                         {(m, e) for m, efforts in jev_router.WORKER_EFFORTS.items() for e in efforts})
        self.assertNotIn('same_account', seen[0]['state'])
        self.assertNotIn('identity', seen[0]['state'])

    def test_failed_assessment_and_missing_credentials_have_no_spawn_arguments(self):
        for outcome in ['uncertain', 'insufficient_information']:
            with self.subTest(outcome=outcome), patch.object(bridge, 'read_credential', return_value='offline-test-token'):
                result = bridge.route(self.request(), ROOT, transport=lambda p, *_: self.answer(p, outcome=outcome))
            self.assertEqual(result['status'], 'refused')
            self.assertNotIn('spawn_agent', result)
            self.assertNotIn('spawn_agent_request', result['receipt'])
        with patch.object(bridge, 'read_credential', return_value=None):
            result = bridge.route(self.request(), ROOT, transport=lambda *_: self.fail('transport must not run'))
        self.assertEqual(result['status'], 'refused')
        self.assertNotIn('spawn_agent', result)
        self.assertEqual(result['receipt']['reason'], 'unavailable')

    def test_invalid_policy_and_metadata_refuse_before_credentials(self):
        request = self.request()
        cases = [{**request, 'ordinary': {'provider': 'codex', 'model': 'gpt-6-luna', 'effort': 'ultra'}},
                 {**request, 'capability_provenance': {**self.capability(), 'source': 'stale_cache'}}]
        for request in cases:
            with patch.object(bridge, 'read_credential', side_effect=AssertionError('credential access')):
                result = bridge.route(request, ROOT, transport=lambda *_: self.fail('transport must not run'))
            self.assertEqual(result['status'], 'refused')
            self.assertNotIn('spawn_agent', result)

    def test_final_spawn_arguments_intersect_native_capabilities_and_owner_caps(self):
        rows = [{'model': m, 'effort': e} for m, efforts in jev_router.WORKER_EFFORTS.items() for e in efforts]
        receipt = {'capability_provenance': 'caller_supplied_tool_metadata',
                   'capability_tool': 'collaboration.spawn_agent', 'spawn_capability_pairs': rows}
        for row in rows:
            self.assertIsNone(bridge.spawn_agent_request(row, receipt))
            with patch.object(bridge, 'read_credential', return_value='offline-test-token'):
                prepared = bridge.route(self.request(), ROOT, transport=lambda p, *_: self.answer(p, model=row['model'], effort=row['effort']))
            self.assertEqual(prepared['spawn_agent'],
                             {'model': row['model'], 'reasoning_effort': row['effort'], 'fork_turns': 'none'})
        for model, effort in [('gpt-6.1-sol', 'xhigh'), ('gpt-6-luna', 'ultra'), ('gpt-6-luna', 'persistent')]:
            self.assertIsNone(bridge.spawn_agent_request({'model': model, 'effort': effort}, receipt))
            self.assertFalse(jev_router.allowed_pair(model, effort))
        self.assertIsNone(bridge.spawn_agent_request(rows[0], {'catalog_pairs': rows}))

    def test_legacy_native_catalog_preserves_preflight_shape_without_tool_impersonation(self):
        import time
        request = self.request()
        request.pop('capability_provenance')
        request['catalog'] = {'same_account': True, 'identity': 'offline-catalog', 'fetched_at': time.time(),
                             'models': [{'model': 'gpt-6-luna', 'supportedReasoningEfforts': ['low', 'medium', 'ultra', 'persistent']}]}
        with patch.object(bridge, 'read_credential', return_value='offline-test-token'):
            result = bridge.route(request, ROOT, transport=lambda p, *_: self.answer(p))
        self.assertEqual(result['status'], 'selected')
        self.assertEqual((result['model'], result['effort']), ('gpt-6-luna', 'medium'))
        self.assertNotIn('spawn_agent', result)
        self.assertEqual({r['effort'] for r in result['receipt']['catalog_pairs']}, {'low', 'medium'})
        with patch.object(bridge, 'read_credential', return_value='offline-test-token'):
            confirmed = bridge.route(request, ROOT, transport=lambda p, *_: self.answer(p, effort='low'))
        self.assertEqual(confirmed['status'], 'fallback')  # Existing Archon wire shape.
        self.assertEqual(confirmed['receipt']['assessment_status'], 'confirmed_ordinary')
        self.assertNotIn('spawn_agent', confirmed)
        for catalog in [{**request['catalog'], 'same_account': False},
                        {**request['catalog'], 'fetched_at': time.time() - 61}]:
            with patch.object(bridge, 'read_credential', side_effect=AssertionError('credential access')):
                result = bridge.route({**request, 'catalog': catalog}, ROOT,
                                      transport=lambda *_: self.fail('invalid catalog must not reach Jev'))
            self.assertEqual(result['status'], 'fallback')
            self.assertNotIn('spawn_agent', result)

    def test_provider_payload_redacts_credential_in_task_and_catalog_descriptions(self):
        import time
        request = self.request()
        request.pop('capability_provenance')
        request['task'] += ' offline-test-token secret=value'
        request['catalog'] = {'same_account': True, 'identity': 'offline-catalog', 'fetched_at': time.time(),
                             'models': [{'model': 'gpt-6-luna', 'description': 'offline-test-token',
                                         'supportedReasoningEfforts': ['low', 'medium']}]}
        seen = []
        with patch.object(bridge, 'read_credential', return_value='offline-test-token'):
            result = bridge.route(request, ROOT, transport=lambda p, *_: seen.append(p) or self.answer(p))
        self.assertEqual(result['status'], 'selected')
        self.assertNotIn('offline-test-token', json.dumps(seen))
        self.assertNotIn('secret=value', json.dumps(seen))
        self.assertNotIn('offline-test-token', json.dumps(result))

    def test_budget_is_optional_and_explicit_values_have_no_8000ms_ceiling(self):
        for budget in ('omitted', None, 7800, 8000, 9001):
            with self.subTest(budget=budget), patch.object(bridge, 'read_credential', return_value='offline-test-token'), \
                    patch.object(bridge.time, 'monotonic', return_value=100.0):
                request = self.request()
                if budget == 'omitted':
                    request.pop('remaining_ms')
                    expected = None
                else:
                    request['remaining_ms'] = budget
                    expected = None if budget is None else 100.0 + budget / 1000
                seen = []
                result = bridge.route(request, ROOT, transport=lambda p, _, deadline:
                    seen.append(deadline) or self.answer(p))
                self.assertEqual(result['status'], 'selected')
                self.assertEqual(seen, [expected])

    def test_invalid_and_expired_budgets_refuse_before_transport(self):
        for budget in (0, -1, float('nan'), float('inf'), True, '8000'):
            with self.subTest(budget=budget), patch.object(bridge, 'read_credential',
                    side_effect=AssertionError('invalid budget must be rejected before credentials')):
                result = bridge.route({**self.request(), 'remaining_ms': budget}, ROOT,
                                      transport=lambda *_: self.fail('invalid budget reached transport'))
                self.assertEqual(result['status'], 'refused')
        with patch.object(bridge.time, 'monotonic', return_value=100.0):
            _, receipt = jev_router.choose('bounded task', None, 'offline-test-token', 'jev-latest',
                99.0, ordinary={'provider': 'codex', 'model': 'gpt-6-luna', 'effort': 'low'},
                capability_provenance=self.capability(),
                transport=lambda *_: self.fail('expired budget reached transport'))
        self.assertEqual(receipt['reason'], 'deadline')

    def test_http_transport_uses_stdlib_default_or_explicit_deadline(self):
        class Child:
            pid, returncode = 321, 0
            def __init__(self, output):
                self.output = output
                self.timeouts = []
            def communicate(self, data=None, **kwargs):
                self.timeouts.append(kwargs.get('timeout'))
                return self.output, None
            def poll(self): return self.returncode
        for deadline, expected_timeout in ((None, None), (100.0, 5.0)):
            child = Child(b'{"ok":true,"result":{}}')
            with self.subTest(deadline=deadline), patch.object(jev_router.subprocess, 'Popen', return_value=child), \
                    patch.object(jev_router.time, 'monotonic', return_value=95.0):
                jev_router.http_choice({'bounded': True}, 'offline-test-token', deadline)
            self.assertEqual(child.timeouts, [expected_timeout])

    def test_child_wait_timeout_maps_to_deadline_and_reaps_child(self):
        class Child:
            pid = 321
            def __init__(self):
                self.calls = 0
                self.returncode = None
                self.killed = False
                self.reaped = False
            def communicate(self, data=None, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired('http child', 5)
                self.returncode = -9
                self.reaped = True
                return b'', None
            def poll(self): return self.returncode
            def kill(self): self.killed = True; self.returncode = -9
        child = Child()
        with patch.object(jev_router.subprocess, 'Popen', return_value=child), \
                patch.object(jev_router.time, 'monotonic', return_value=95.0), \
                self.assertRaises(jev_router.TransportFailure) as raised:
            jev_router.http_choice({'bounded': True}, 'offline-test-token', 100.0)
        self.assertEqual(raised.exception.kind, 'deadline')
        self.assertTrue(raised.exception.transport['reaped'])
        self.assertTrue(child.killed)
        self.assertTrue(child.reaped)
        self.assertEqual(child.calls, 2)

    def test_http_request_uses_stdlib_default_or_remaining_budget(self):
        class Response:
            status = 200
            headers = {}
            def __init__(self): self.chunks = iter((b'{"ok":true}', b''))
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read1(self, _): return next(self.chunks)
        for deadline, expected in ((None, ((), {})), (110.0, ((), {'timeout': 10.0}))):
            with self.subTest(deadline=deadline), patch.object(jev_router.time, 'monotonic', return_value=100.0), \
                    patch.object(jev_router.urllib.request, 'urlopen', return_value=Response()) as open_url:
                jev_router._http_once({}, 'offline-test-token', deadline)
            self.assertEqual(open_url.call_args.args[1:], ())
            self.assertEqual(open_url.call_args.kwargs, expected[1])

    def test_parent_cancellation_kills_and_reaps_http_child(self):
        class Child:
            pid = 321
            def __init__(self, signal_in_communicate=True):
                self.calls = 0; self.killed = False; self.returncode = None; self.reaped = False
                self.signal_in_communicate = signal_in_communicate
            def communicate(self, data=None, **kwargs):
                self.calls += 1
                if self.calls == 1 and self.signal_in_communicate:
                    os.kill(os.getpid(), signal.SIGTERM)
                    self.returncode = 0
                    self.reaped = True
                    return b'{"ok":true,"result":{}}', None
                self.returncode = -9
                self.reaped = True
                return b'', None
            def poll(self): return self.returncode
            def kill(self): self.killed = True; self.returncode = -9
        original = signal.getsignal(signal.SIGTERM)
        for prior_kind in ('return', 'raise', 'timeout_raise', 'default', 'pending', 'pending_raise', 'ignored'):
            with self.subTest(prior_kind=prior_kind):
                child = Child(signal_in_communicate=prior_kind not in ('pending', 'pending_raise'))
                received = []
                prior_timeout = subprocess.TimeoutExpired('prior handler', 1)
                if prior_kind == 'default':
                    prior_handler = signal.SIG_DFL
                elif prior_kind == 'ignored':
                    prior_handler = signal.SIG_IGN
                elif prior_kind in ('return', 'pending'):
                    def prior_handler(signum, frame): received.append(signum)
                elif prior_kind == 'timeout_raise':
                    def prior_handler(signum, frame):
                        received.append(signum)
                        raise prior_timeout
                else:
                    def prior_handler(signum, frame):
                        received.append(signum)
                        raise ValueError('prior handler failure')
                signal.signal(signal.SIGTERM, prior_handler)
                def launch(*_args, **_kwargs):
                    if prior_kind in ('pending', 'pending_raise'):
                        os.kill(os.getpid(), signal.SIGTERM)
                    return child
                try:
                    with patch.object(jev_router.subprocess, 'Popen', side_effect=launch), \
                            patch.object(jev_router.time, 'monotonic', return_value=100.0):
                        if prior_kind == 'return':
                            with self.assertRaises(jev_router.TransportFailure) as raised:
                                jev_router.http_choice({'bounded': True}, 'offline-test-token', None)
                            self.assertEqual(raised.exception.kind, 'cancelled')
                            caller_continued = True
                        elif prior_kind == 'pending':
                            with self.assertRaises(jev_router.TransportFailure) as raised:
                                jev_router.http_choice({'bounded': True}, 'offline-test-token', None)
                            self.assertEqual(raised.exception.kind, 'cancelled')
                            caller_continued = True
                        elif prior_kind == 'pending_raise':
                            with self.assertRaisesRegex(ValueError, 'prior handler failure'):
                                jev_router.http_choice({'bounded': True}, 'offline-test-token', None)
                            caller_continued = True
                        elif prior_kind == 'raise':
                            with self.assertRaisesRegex(ValueError, 'prior handler failure'):
                                jev_router.http_choice({'bounded': True}, 'offline-test-token', None)
                            caller_continued = True
                        elif prior_kind == 'timeout_raise':
                            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                                jev_router.http_choice({'bounded': True}, 'offline-test-token', None)
                            self.assertIs(raised.exception, prior_timeout)
                            caller_continued = True
                        elif prior_kind == 'ignored':
                            result = jev_router.http_choice({'bounded': True}, 'offline-test-token', None)
                            self.assertTrue(result)
                            caller_continued = True
                        else:
                            with self.assertRaises(SystemExit) as raised:
                                jev_router.http_choice({'bounded': True}, 'offline-test-token', None)
                            self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
                            caller_continued = False
                    self.assertIs(signal.getsignal(signal.SIGTERM), prior_handler)
                    self.assertEqual(received, [] if prior_kind in ('default', 'ignored') else [signal.SIGTERM])
                    self.assertEqual(child.killed, prior_kind != 'ignored')
                    self.assertTrue(child.reaped)
                    self.assertEqual(child.calls, 1 if prior_kind in ('pending', 'pending_raise', 'ignored') else 2)
                    self.assertEqual(caller_continued, prior_kind != 'default')
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.reaped = True
                    signal.signal(signal.SIGTERM, original)


if __name__ == '__main__':
    unittest.main()
