"""CPI-411 controls through the real outer command and bound bridge. No network."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
WORK = BASE / 'work/native-router-tests'
WORK.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE / 'template/factory'))
sys.path.insert(0, str(BASE / 'bin'))
import consumer
import jev_prepared_bridge as bridge
import jev_router
spec = importlib.util.spec_from_file_location('factory_entry', BASE / 'bin/factory.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='controls-', dir=WORK)
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.a = self.project('a')
        self.b = self.project('b')
        self.request = self.bound_request(self.a)
        self.credential = patch.object(bridge, 'read_credential', return_value='offline-fixture-key')
        self.credential.start()
        self.addCleanup(self.credential.stop)

    def project(self, name):
        root = self.base / name
        root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        (root / '_bmad/custom').mkdir(parents=True)
        (root / '_bmad/custom/ticketing-store-config.toml').write_text(
            '[tickets]\nstore="linear"\nproject="project-' + name + '"\n')
        (root / '.factory').mkdir()
        (root / '.factory/project-context.md').write_text('Context ' + name)
        (root / consumer.SETTINGS).write_text(json.dumps({
            'project_binding': consumer.project_binding(root),
            'jev_router': {'key_file': str(root / 'missing-private-key')}}))
        return root

    def bound_request(self, root):
        return {'schema': 'CPI411SUBAGENTV1', 'caller_id': 'native:fixture', 'task_id': 'CPI-411:controls',
                'project_binding': consumer.project_binding(root), 'task': 'Bounded offline control',
                'ordinary': {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'medium'},
                'remaining_ms': 2000,
                'capability_provenance': {'source': 'current_runtime_tool_schema',
                    'tool': 'collaboration.spawn_agent', 'caller_id': 'native:fixture', 'captured_at': time.time(),
                    'models': [{'model': 'gpt-6-luna', 'reasoning_efforts': ['low', 'medium', 'max']},
                               {'model': 'gpt-6.1-sol', 'reasoning_efforts': ['low', 'medium', 'high', 'xhigh']},
                               {'model': 'gpt-5.6-sol', 'reasoning_efforts': ['medium']}]}}

    def answer(self, demand='bounded_local', confidence=1):
        values = {'task_kind': 'known_pattern_implementation', 'reasoning_demand': demand,
                  'verification_difficulty': 'integration_checks', 'material_consequence': 'bounded_reversible'}
        return {'answers': {name: {'type': 'choice', 'choice': choice, 'confidence': confidence,
                'probabilities': {option: 1.0 if option == choice else 0.0 for option in jev_router.QUESTIONS[name]['criteria']}}
                for name, choice in values.items()}, 'model': 'jev-fixture', 'usage': {'input_tokens': 10, 'output_tokens': 4}}

    def command(self, request=None, root=None, transport=None, work='work/control'):
        request = self.request if request is None else request
        root = self.a if root is None else root
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['factory', 'route-subagent', '--caller-id', 'native:fixture', '--work-dir', work]), \
             patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))), \
             patch.object(consumer, 'project_root', return_value=root), \
             patch.object(bridge, 'http_choice', side_effect=transport or (lambda *_: self.answer())), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = entry.main()
        return code, json.loads(output.getvalue()) if output.getvalue() else None, errors.getvalue()

    def test_selected_exact_supported_args_persist_and_correlate(self):
        code, result, _ = self.command()
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'selected')
        self.assertEqual(result['spawn_agent'], {'model': 'gpt-6-luna', 'reasoning_effort': 'medium', 'fork_turns': 'none'})
        self.assertEqual(json.loads(Path(result['receipt_path']).read_text()), result)
        self.assertEqual(Path(result['receipt_path']).stat().st_mode & 0o777, 0o600)
        for key in ['preparation_id', 'caller_id', 'project_binding', 'capability_identity_sha256']:
            self.assertEqual(result[key], result['dispatch_receipt'][key])
        self.assertEqual(result['dispatch'], 'not_invoked')
        self.assertIsNone(result['observed_effort'])
        pairs = result['receipt']['spawn_capability_pairs']
        self.assertNotIn({'model': 'gpt-6.1-sol', 'effort': 'xhigh'}, pairs)
        self.assertFalse(any(p['model'] == 'gpt-5.6-sol' for p in pairs))

    def test_explicit_uses_same_receipt_without_credential_or_http(self):
        self.request['explicit_choice'] = {'model': 'gpt-6-luna', 'effort': 'max'}
        with patch.object(bridge, 'read_credential', side_effect=AssertionError('credential accessed')):
            code, result, _ = self.command(transport=lambda *_: self.fail('HTTP accessed'))
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'explicit')
        self.assertEqual(result['receipt']['attempts'], 0)
        self.assertEqual(json.loads(Path(result['receipt_path']).read_text()), result)
        self.request['explicit_choice'] = {'model': 'gpt-6.1-sol', 'effort': 'xhigh'}
        code, result, _ = self.command(transport=lambda *_: self.fail('HTTP accessed'))
        self.assertEqual((code, result['status']), (1, 'refused'))
        self.assertNotIn('spawn_agent', result)

    def test_ordinary_confirmed_distinct_from_unavailable(self):
        _, result, _ = self.command(transport=lambda *_: self.answer('diagnostic_or_cross_interface'))
        self.assertEqual(result['status'], 'confirmed_ordinary')
        self.assertEqual(result['receipt']['assessment_status'], 'confirmed_ordinary')
        self.assertEqual(result['dispatch_receipt']['status'], 'ordinary_confirmed')
        self.assertEqual(result['receipt']['attempts'], 1)

    def test_optional_failures_keep_validated_ordinary(self):
        cases = [('timeout', lambda *_: (_ for _ in ()).throw(TimeoutError()), 'routing_TimeoutError'),
                 ('uncertain', lambda *_: self.answer(confidence=.6), 'assessment_uncertain'),
                 ('malformed', lambda *_: {'answers': {}}, 'assessment_malformed')]
        for name, transport, reason in cases:
            with self.subTest(name=name):
                code, result, _ = self.command(transport=transport)
                self.assertEqual(code, 0)
                self.assertEqual(result['status'], 'fallback')
                self.assertEqual(result['receipt']['reason'], reason)
                self.assertEqual(result['spawn_agent']['model'], 'gpt-6.1-sol')
                self.assertEqual(result['dispatch_receipt']['status'], 'ordinary_fallback')
        self.request['capability_provenance']['models'] = [self.request['capability_provenance']['models'][1]]
        _, result, _ = self.command()
        self.assertEqual(result['receipt']['reason'], 'mapped_pair_unavailable')
        for credential, reason in [(None, 'unavailable'), (PermissionError(), 'credential_unavailable')]:
            with patch.object(bridge, 'read_credential', **({'side_effect': credential} if isinstance(credential, Exception) else {'return_value': credential})):
                code, result, _ = self.command(transport=lambda *_: self.fail('HTTP accessed'))
            self.assertEqual(code, 0)
            self.assertEqual(result['receipt']['reason'], reason)
            self.assertEqual(result['spawn_agent']['reasoning_effort'], 'medium')
            self.assertEqual(result['receipt']['attempts'], 0)

    def test_invalid_ordinary_refuses_before_optional_access(self):
        for model, effort in [('gpt-5.6-sol', 'medium'), ('gpt-6.1-sol', 'xhigh'), ('gpt-6-luna', 'high')]:
            with self.subTest(pair=(model, effort)):
                self.request['ordinary'].update(model=model, effort=effort)
                with patch.object(bridge, 'read_credential', side_effect=AssertionError('credential accessed')):
                    code, result, _ = self.command(transport=lambda *_: self.fail('HTTP accessed'))
                self.assertEqual(code, 1)
                self.assertNotIn('spawn_agent', result)

    def test_receipt_failure_keeps_exact_request_with_truthful_caller_fallback(self):
        for explicit in (False, True):
            request = copy.deepcopy(self.request)
            if explicit:
                request['explicit_choice'] = {'model': 'gpt-6-luna', 'effort': 'max'}
            with patch.object(bridge.os, 'open', side_effect=PermissionError()):
                code, result, _ = self.command(request)
            self.assertEqual(code, 0)
            self.assertEqual(result['persistence']['status'], 'failed')
            self.assertIn('before dispatch', result['persistence']['caller_fallback'])
            self.assertIn('spawn_agent', result)
            self.assertEqual(result['spawn_agent']['model'], 'gpt-6-luna' if explicit else 'gpt-6.1-sol')
            if not explicit:
                self.assertEqual(result['status'], 'fallback')
                self.assertEqual(result['receipt']['assessment_status'], 'selected')
                self.assertEqual(result['receipt']['reason'], 'native_bridge_receipt_unavailable')
            self.assertFalse(Path(result['receipt_path']).exists())

    def test_identity_binding_capability_and_workdir_corruption_refuse(self):
        cases = []
        for path, value in [(('caller_id',), 'other'), (('project_binding', 'root'), str(self.b)),
                            (('capability_provenance', 'caller_id'), 'other'),
                            (('capability_provenance', 'captured_at'), time.time() - 61),
                            (('capability_provenance', 'captured_at'), time.time() + 61),
                            (('capability_provenance', 'tool'), 'wrong'),
                            (('schema',), 'wrong'), (('task',), ''), (('task',), 'x' * 8193)]:
            request = copy.deepcopy(self.request)
            obj = request
            for part in path[:-1]: obj = obj[part]
            obj[path[-1]] = value
            cases.append(request)
        for request in cases:
            with self.subTest(request=request):
                code, result, _ = self.command(request, transport=lambda *_: self.fail('HTTP accessed'))
                self.assertEqual((code, result), (1, None))
        self.assertEqual(self.command(root=self.b)[0], 1)
        for path in ['work', '../escape', str(self.b / 'work/outside')]:
            self.assertEqual(self.command(work=path)[0], 1)
        (self.a / 'work').mkdir(exist_ok=True)
        (self.a / 'work/escape').symlink_to(self.b, target_is_directory=True)
        self.assertEqual(self.command(work='work/escape/receipt')[0], 1)
        settings_path = self.a / consumer.SETTINGS
        settings = json.loads(settings_path.read_text())
        settings['project_binding']['project'] = 'wrong'
        settings_path.write_text(json.dumps(settings))
        self.assertEqual(self.command()[0], 1)

    def test_real_subprocess_command_two_bindings_and_unreadable_key(self):
        env = {key: value for key, value in os.environ.items() if key != 'TYPESAFE_API_KEY'}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        roots = []
        for root in (self.a, self.b):
            request = self.bound_request(root)
            request['explicit_choice'] = {'model': 'gpt-6-luna', 'effort': 'low'}
            argv = [sys.executable, '-B', str(BASE / 'bin/factory.py'), 'route-subagent',
                    '--caller-id', 'native:fixture', '--work-dir', 'work/subprocess']
            result = subprocess.run(argv, input=json.dumps(request), text=True, capture_output=True, cwd=root, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            roots.append(output['project_binding']['root'])
            self.assertEqual(output['status'], 'explicit')
            request.pop('explicit_choice')
            # Existing insecure key and missing key are distinct real IO controls.
            for insecure in (False, True):
                key = root / 'missing-private-key'
                if insecure:
                    key.write_text('offline-control-token')
                    key.chmod(0o644)
                result = subprocess.run(argv, input=json.dumps(request), text=True, capture_output=True, cwd=root, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output['receipt']['reason'], 'credential_unavailable')
                self.assertEqual(output['receipt']['attempts'], 0)
                self.assertEqual(output['spawn_agent']['model'], 'gpt-6.1-sol')
        self.assertNotEqual(*roots)


if __name__ == '__main__':
    unittest.main()
