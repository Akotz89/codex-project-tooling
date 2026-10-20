"""CPI-413 assessment-first controls through the real outer command and bound bridge. No network."""
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
WORK = BASE / 'work/jev-assessed-handoffs-20261020/native-router-tests'
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
                'project_binding': consumer.project_binding(root), 'task': 'Implement the specified local parser repair; cause and plan known, isolated source only, no service changes. Dependencies: parser and caller fixture. Acceptance: run parser assertions and integration checks.',
                'ordinary': {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'medium'},
                'remaining_ms': 2000,
                'capability_provenance': {'source': 'current_runtime_tool_schema',
                    'tool': 'collaboration.spawn_agent', 'caller_id': 'native:fixture', 'captured_at': time.time(),
                    'models': [{'model': 'gpt-6-luna', 'reasoning_efforts': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'persistent']},
                               {'model': 'gpt-6.1-sol', 'reasoning_efforts': ['low', 'medium', 'high', 'xhigh']},
                               {'model': 'gpt-5.6-sol', 'reasoning_efforts': ['medium']}]}}

    def answer(self, payload, model='gpt-6-luna', effort='medium', confidence=1, outcome=None):
        options = payload['questions']['worker_pair']['criteria']
        choice = outcome or next(key for key in options if key == 'pair_' + __import__('hashlib').sha256(f'{model}\0{effort}'.encode()).hexdigest()[:16])
        return {'answers': {'worker_pair': {'type': 'choice', 'choice': choice, 'confidence': confidence,
                'probabilities': {option: 1.0 if option == choice else 0.0 for option in options}}},
                'model': 'jev-fixture', 'usage': {'input_tokens': 10, 'output_tokens': 4}}

    def override(self, model='gpt-6-luna', effort='max'):
        return {'model': model, 'effort': effort, 'source': 'direct_human_instruction',
                'instruction_quote': f'Use {model} at {effort} for this bounded worker.',
                'instruction_reference': {'kind': 'chat_turn', 'chat_id': 'native:fixture', 'turn_id': 'human:1'}}

    def no_args(self, result):
        self.assertNotIn('spawn_agent', result)
        self.assertNotIn('dispatch_receipt', result)
        self.assertNotIn('spawn_agent_request', result['receipt'])
        self.assertNotIn('model', result)
        self.assertNotIn('effort', result)

    def command(self, request=None, root=None, transport=None, work='work/control'):
        request = self.request if request is None else request
        root = self.a if root is None else root
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['factory', 'route-subagent', '--caller-id', 'native:fixture', '--work-dir', work]), \
             patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))), \
             patch.object(consumer, 'project_root', return_value=root), \
             patch.object(bridge, 'http_choice', side_effect=transport or (lambda payload, *_: self.answer(payload))), \
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

    def test_public_provider_payload_preserves_only_allowed_descriptors_with_redaction(self):
        models = self.request['capability_provenance']['models']
        models[0]['description'] = 'Fast and affordable model for easier tasks; offline-fixture-key password=fixture-secret'
        models[0]['reasoning_effort_descriptions'] = {
            'low': 'Current low effort description.',
            'medium': 'Current medium effort offline-fixture-key description.',
            'high': 'h' * 300,
            'max': 'm' * 250 + 'offline-fixture-key',
            'ultra': 'FORBIDDEN_LUNA_ULTRA', 'persistent': 'FORBIDDEN_LUNA_PERSISTENT',
            'not_in_efforts': 'UNADVERTISED_EFFORT'}
        models[1]['description'] = 'Latest workhorse model for coding and everyday work'
        models[1]['reasoning_effort_descriptions'] = {'low': 'Current Sol low effort description.', 'xhigh': 'FORBIDDEN_SOL_XHIGH'}
        models[2]['description'] = 'FORBIDDEN_MODEL'
        seen = []
        code, result, _ = self.command(transport=lambda p, *_: seen.append(p) or self.answer(p, effort='max'))
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1)
        received = {(row['model'], row['effort']): row for row in seen[0]['state']['allowed_catalog']}
        expected = {}
        for model, efforts in jev_router.WORKER_EFFORTS.items():
            for effort in efforts:
                description = ('Fast and affordable model for easier tasks; [REDACTED] [REDACTED]'
                               if model == 'gpt-6-luna' else 'Latest workhorse model for coding and everyday work')
                effort_text = {('gpt-6-luna', 'low'): 'Current low effort description.',
                               ('gpt-6-luna', 'medium'): 'Current medium effort [REDACTED] description.',
                               ('gpt-6-luna', 'high'): 'h' * 256,
                               ('gpt-6-luna', 'max'): 'm' * 250 + '[REDAC',
                               ('gpt-6.1-sol', 'low'): 'Current Sol low effort description.'}.get((model, effort), '')
                expected[(model, effort)] = {'model': model, 'effort': effort,
                                             'description': description, 'effort_description': effort_text}
        self.assertEqual(received, expected)
        # The same descriptors reach the actual typed options used to select this pair.
        options = seen[0]['questions']['worker_pair']['criteria']
        for key, pair in jev_router.collaboration_capability_pairs(self.request['capability_provenance']).items():
            row = expected[(pair['model'], pair['effort'])]
            self.assertIn('Model capability: ' + row['description'] + '.', options[key])
            self.assertIn('Effort capability: ' + row['effort_description'] + '.', options[key])
        for value in ['offline-fixture-key', 'fixture-secret', 'FORBIDDEN_', 'UNADVERTISED_EFFORT']:
            self.assertNotIn(value, json.dumps(seen))
            self.assertNotIn(value, json.dumps(result))
        self.assertEqual(result['spawn_agent'], {'model': 'gpt-6-luna', 'reasoning_effort': 'max', 'fork_turns': 'none'})
        self.assertEqual(json.loads(Path(result['receipt_path']).read_text()), result)

    def test_public_provider_payload_ignores_malformed_optional_descriptors(self):
        for bad in [None, 7, False, [], {}, '   ']:
            with self.subTest(descriptor=bad):
                request = copy.deepcopy(self.request)
                models = request['capability_provenance']['models']
                models[0]['description'] = bad
                models[0]['reasoning_effort_descriptions'] = {effort: bad for effort in jev_router.WORKER_EFFORTS['gpt-6-luna']}
                models[1]['description'] = bad
                models[1]['reasoning_effort_descriptions'] = ['malformed mapping']
                seen = []
                code, result, _ = self.command(request, transport=lambda p, *_: seen.append(p) or self.answer(p))
                self.assertEqual(code, 0)
                rows = seen[0]['state']['allowed_catalog']
                self.assertEqual({(r['model'], r['effort']) for r in rows},
                                 {(m, e) for m, efforts in jev_router.WORKER_EFFORTS.items() for e in efforts})
                self.assertTrue(all(r['description'] == r['effort_description'] == '' for r in rows))
                self.assertEqual(result['spawn_agent']['reasoning_effort'], 'medium')

    def test_explicit_uses_same_receipt_without_credential_or_http(self):
        self.request['explicit_choice'] = self.override()
        with patch.object(bridge, 'read_credential', side_effect=AssertionError('credential accessed')) as credential:
            code, result, _ = self.command(transport=lambda *_: self.fail('HTTP accessed'))
        credential.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'human_override')
        self.assertEqual(result['receipt']['ordinary_worker'], self.request['ordinary'])
        self.assertEqual(result['spawn_agent'], {'model': 'gpt-6-luna', 'reasoning_effort': 'max', 'fork_turns': 'none'})
        self.assertEqual(result['receipt']['attempts'], 0)
        self.assertEqual(json.loads(Path(result['receipt_path']).read_text()), result)
        self.request['explicit_choice'] = self.override('gpt-6.1-sol', 'xhigh')
        code, result, _ = self.command(transport=lambda *_: self.fail('HTTP accessed'))
        self.assertEqual((code, result['status']), (1, 'refused'))
        self.assertNotIn('spawn_agent', result)

    def test_valid_human_override_cannot_bypass_ordinary_policy_or_current_capability(self):
        cases = [
            {'provider': 'codex', 'model': 'gpt-5.6-sol', 'effort': 'medium'},
            {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'xhigh'},
            {'provider': 'codex', 'model': 'gpt-6-luna', 'effort': 'ultra'},
            {'provider': 'codex', 'model': 'gpt-6-luna', 'effort': 'persistent'},
            # Sol high is policy-permitted but absent from this current capability snapshot.
            {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'},
            {'provider': 'foreign', 'model': 'gpt-6-luna', 'effort': 'low'},
            None,
        ]
        for ordinary in cases:
            with self.subTest(ordinary=ordinary):
                request = copy.deepcopy(self.request)
                request['ordinary'] = ordinary
                request['explicit_choice'] = self.override()
                request['capability_provenance']['models'][1]['reasoning_efforts'] = ['low', 'medium']
                with patch.object(bridge, 'read_credential', side_effect=AssertionError('credential accessed')) as credential:
                    code, result, _ = self.command(request, transport=lambda *_: self.fail('HTTP accessed'))
                credential.assert_not_called()
                self.assertEqual((code, result['status']), (1, 'refused'))
                self.assertEqual(result['receipt']['reason'], 'worker_model_policy')
                self.no_args(result)
                self.assertEqual(result['persistence']['status'], 'persisted')
                self.assertEqual(json.loads(Path(result['receipt_path']).read_text()), result)

    def test_ordinary_confirmed_distinct_from_unavailable(self):
        _, result, _ = self.command(transport=lambda payload, *_: self.answer(payload, model='gpt-6.1-sol'))
        self.assertEqual(result['status'], 'confirmed_ordinary')
        self.assertEqual(result['receipt']['assessment_status'], 'confirmed_ordinary')
        self.assertEqual(result['dispatch_receipt']['status'], 'ordinary_confirmed')
        self.assertEqual(result['receipt']['attempts'], 1)

    def test_assessment_failures_persist_reason_without_launch_arguments(self):
        cases = [('timeout', lambda *_: (_ for _ in ()).throw(TimeoutError()), 'routing_TimeoutError'),
                 ('transport', lambda *_: (_ for _ in ()).throw(jev_router.TransportFailure('http_or_protocol', 1, True)), 'transport_http_or_protocol'),
                 ('uncertain', lambda p, *_: self.answer(p, outcome='uncertain'), 'assessment_uncertain'),
                 ('insufficient', lambda p, *_: self.answer(p, outcome='insufficient_information'), 'assessment_insufficient'),
                 ('malformed', lambda *_: {'answers': {}}, 'assessment_malformed')]
        for name, transport, reason in cases:
            with self.subTest(name=name):
                code, result, _ = self.command(transport=transport)
                self.assertEqual(code, 1)
                self.assertEqual(result['status'], 'refused')
                self.assertEqual(result['receipt']['reason'], reason)
                self.no_args(result)
                self.assertEqual(json.loads(Path(result['receipt_path']).read_text()), result)
        for credential, reason in [(None, 'unavailable'), (PermissionError(), 'credential_unavailable')]:
            with patch.object(bridge, 'read_credential', **({'side_effect': credential} if isinstance(credential, Exception) else {'return_value': credential})):
                code, result, _ = self.command(transport=lambda *_: self.fail('HTTP accessed'))
            self.assertEqual(code, 1)
            self.assertEqual(result['receipt']['reason'], reason)
            self.no_args(result)
            self.assertEqual(result['receipt']['attempts'], 0)

    def test_every_permitted_pair_is_directly_selectable(self):
        for model, efforts in jev_router.WORKER_EFFORTS.items():
            for effort in efforts:
                with self.subTest(pair=(model, effort)):
                    seen = []
                    def transport(payload, *_):
                        seen.append(payload)
                        return self.answer(payload, model=model, effort=effort)
                    code, result, _ = self.command(transport=transport)
                    self.assertEqual(code, 0)
                    self.assertEqual(result['spawn_agent'], {'model': model, 'reasoning_effort': effort, 'fork_turns': 'none'})
                    self.assertEqual(result['receipt']['prepared_pair'], {'model': model, 'effort': effort})
                    self.assertEqual(result['receipt']['attempts'], 1)
                    self.assertEqual(len(seen), 1)
                    self.assertEqual(json.loads(Path(result['receipt_path']).read_text()), result)
                    self.assertEqual(set(seen[0]['questions']), {'worker_pair'})

    def test_low_preference_confidence_is_retained_and_ties_refuse(self):
        def split(payload, *_):
            answer = self.answer(payload, confidence=.1)
            item = answer['answers']['worker_pair']
            other = next(k for k in item['probabilities'] if k.startswith('pair_') and k != item['choice'])
            item['probabilities'][item['choice']] = .51
            item['probabilities'][other] = .49
            return answer
        code, result, _ = self.command(transport=split)
        self.assertEqual(code, 0)
        self.assertEqual(result['receipt']['assessments']['worker_pair']['confidence'], .1)
        def tied(payload, *_):
            answer = split(payload)
            item = answer['answers']['worker_pair']
            item['probabilities'] = {k: .5 if v else 0 for k, v in item['probabilities'].items()}
            return answer
        code, result, _ = self.command(transport=tied)
        self.assertEqual(code, 1)
        self.assertEqual(result['receipt']['reason'], 'assessment_uncertain')
        self.no_args(result)

    def test_malformed_and_unsupported_provider_answers_refuse(self):
        def mutate(payload, mutation):
            answer = self.answer(payload)
            mutation(answer['answers']['worker_pair'])
            return answer
        mutations = [lambda i: i.update(choice='gpt-6-luna:max'),
                     lambda i: i.update(choice='pair_unsupported'),
                     lambda i: i.update(type='score'), lambda i: i.update(confidence=True),
                     lambda i: i.update(confidence=float('nan')),
                     lambda i: i['probabilities'].pop(i['choice']),
                     lambda i: i['probabilities'].update(foreign=0),
                     lambda i: i['probabilities'].update({i['choice']: .1}),
                     lambda i: i['probabilities'].update({i['choice']: True}),
                     lambda i: i['probabilities'].update({i['choice']: float('inf')})]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                code, result, _ = self.command(transport=lambda p, *_: mutate(p, mutation))
                self.assertEqual(code, 1)
                self.assertEqual(result['receipt']['reason'], 'assessment_malformed')
                self.no_args(result)
        def below_maximum(payload, *_):
            answer = self.answer(payload)
            item = answer['answers']['worker_pair']
            other = next(k for k in item['probabilities'] if k.startswith('pair_') and k != item['choice'])
            item['probabilities'][item['choice']] = .4
            item['probabilities'][other] = .6
            return answer
        code, result, _ = self.command(transport=below_maximum)
        self.assertEqual(code, 1)
        self.no_args(result)
        # A choice valid in some other capability set is still invalid here.
        self.request['capability_provenance']['models'] = [self.request['capability_provenance']['models'][1]]
        def foreign(payload, *_):
            answer = self.answer(payload, model='gpt-6.1-sol')
            answer['answers']['worker_pair']['choice'] = 'pair_' + __import__('hashlib').sha256(b'gpt-6-luna\0medium').hexdigest()[:16]
            return answer
        code, result, _ = self.command(transport=foreign)
        self.assertEqual(code, 1)
        self.no_args(result)

    def test_human_override_requires_specific_attribution_before_key_access(self):
        invalid = [{'model': 'gpt-6-luna', 'effort': 'max'},
                   {**self.override(), 'source': 'caller_default'},
                   {**self.override(), 'instruction_quote': '  '},
                   {**self.override(), 'instruction_reference': {}},
                   {**self.override(), 'instruction_reference': {'kind': 'chat_turn', 'chat_id': '', 'turn_id': '1'}},
                   {**self.override(), 'instruction_reference': {'kind': 'governing_instruction', 'path': '/not-existing-instruction'}}]
        for choice in invalid:
            self.request['explicit_choice'] = choice
            with patch.object(bridge, 'read_credential', side_effect=AssertionError('credential accessed')):
                code, result, _ = self.command(transport=lambda *_: self.fail('HTTP accessed'))
            self.assertEqual(code, 1)
            self.no_args(result)
        instruction = self.a / 'AGENTS.md'
        instruction.write_text('Use gpt-6-luna at max for this bounded worker.')
        self.request['explicit_choice'] = {**self.override(), 'instruction_reference': {'kind': 'governing_instruction', 'path': str(instruction)}}
        code, result, _ = self.command(transport=lambda *_: self.fail('HTTP accessed'))
        self.assertEqual(code, 0)
        self.assertEqual(result['receipt']['assessment_status'], 'unassessed_human_override')
        self.assertEqual(result['receipt']['human_override'], {k: v for k, v in self.request['explicit_choice'].items() if k not in {'model', 'effort'}})

    def test_invalid_ordinary_refuses_before_optional_access(self):
        for model, effort in [('gpt-5.6-sol', 'medium'), ('gpt-6.1-sol', 'xhigh'), ('gpt-6-luna', 'ultra')]:
            with self.subTest(pair=(model, effort)):
                self.request['ordinary'].update(model=model, effort=effort)
                with patch.object(bridge, 'read_credential', side_effect=AssertionError('credential accessed')):
                    code, result, _ = self.command(transport=lambda *_: self.fail('HTTP accessed'))
                self.assertEqual(code, 1)
                self.assertNotIn('spawn_agent', result)

    def test_receipt_failure_withholds_all_arguments_and_preserves_chosen_evidence(self):
        for explicit in (False, True):
            request = copy.deepcopy(self.request)
            if explicit:
                request['explicit_choice'] = self.override()
            for failing in ('open', 'fsync'):
                with self.subTest(explicit=explicit, failing=failing), patch.object(bridge.os, failing, side_effect=PermissionError()):
                    code, result, _ = self.command(request)
                self.assertEqual(code, 1)
                self.assertEqual(result['persistence']['status'], 'failed')
                self.assertEqual(result['status'], 'refused')
                self.no_args(result)
                self.assertEqual(result['receipt']['prepared_pair'], {'model': 'gpt-6-luna', 'effort': 'max' if explicit else 'medium'})
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

    def test_in_project_task_symlink_refused_by_real_outer_command(self):
        work = self.a / 'work'
        target = work / 'other-task'
        target.mkdir(parents=True)
        link = work / 'task-link'
        link.symlink_to(target, target_is_directory=True)
        request = copy.deepcopy(self.request)
        request['explicit_choice'] = self.override(effort='low')
        env = {key: value for key, value in os.environ.items() if key != 'TYPESAFE_API_KEY'}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        for supplied in ['work/task-link', 'work/task-link/receipt', str(link),
                         str(link / 'receipt'), 'work/task-link/../unrelated']:
            with self.subTest(work_dir=supplied):
                process = subprocess.run([
                    sys.executable, '-B', str(BASE / 'bin/factory.py'), 'route-subagent',
                    '--caller-id', 'native:fixture', '--work-dir', supplied],
                    input=json.dumps(request), text=True, capture_output=True, cwd=self.a, env=env)
                self.assertEqual(process.returncode, 1, process.stdout)
                self.assertEqual(process.stdout, '')
                self.assertIn('must not contain symlinks', process.stderr)
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((work / 'unrelated').exists())

    def test_real_subprocess_command_two_bindings_and_unreadable_key(self):
        env = {key: value for key, value in os.environ.items() if key != 'TYPESAFE_API_KEY'}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        roots = []
        for root in (self.a, self.b):
            request = self.bound_request(root)
            request['explicit_choice'] = self.override(effort='low')
            argv = [sys.executable, '-B', str(BASE / 'bin/factory.py'), 'route-subagent',
                    '--caller-id', 'native:fixture', '--work-dir', 'work/subprocess']
            result = subprocess.run(argv, input=json.dumps(request), text=True, capture_output=True, cwd=root, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            roots.append(output['project_binding']['root'])
            self.assertEqual(output['status'], 'human_override')
            request.pop('explicit_choice')
            # Existing insecure key and missing key are distinct real IO controls.
            for insecure in (False, True):
                key = root / 'missing-private-key'
                if insecure:
                    key.write_text('offline-control-token')
                    key.chmod(0o644)
                result = subprocess.run(argv, input=json.dumps(request), text=True, capture_output=True, cwd=root, env=env)
                self.assertEqual(result.returncode, 1, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output['receipt']['reason'], 'credential_unavailable')
                self.assertEqual(output['receipt']['attempts'], 0)
                self.no_args(output)
        self.assertNotEqual(*roots)


if __name__ == '__main__':
    unittest.main()
