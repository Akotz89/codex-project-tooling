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
import native_hook
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

    def command(self, request=None, root=None, transport=None, work='work/control', caller_id='native:fixture'):
        request = self.request if request is None else request
        root = self.a if root is None else root
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['factory', 'route-subagent', '--caller-id', caller_id, '--work-dir', work]), \
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

    def test_actual_payload_has_separate_sourced_economics_and_unknowns(self):
        seen = []
        code, result, _ = self.command(transport=lambda p, *_: seen.append(p) or self.answer(p))
        self.assertEqual(code, 0)
        payload = seen[0]
        context = payload['state']['economic_context']
        self.assertEqual(context['version'], 'CPI404_COST_CONTEXT_20261021_V1')
        self.assertEqual(payload['state']['task'], self.request['task'])
        self.assertEqual(set(payload['questions']), {'worker_pair'})
        panels = {(p['model'], p['benchmark']): p for p in context['panels']}
        self.assertEqual(len(panels), 4)
        for model, efforts in jev_router.WORKER_EFFORTS.items():
            for benchmark in ['VulcanBench Frontier v4 retired-binary parity', 'DeepSWE v1.1 Score']:
                panel = panels[model, benchmark]
                self.assertEqual({row[0] for row in panel['rows']}, efforts)
                self.assertTrue(panel['source_url'].startswith('https://'))
                self.assertTrue(panel['accessed'].startswith('2026-10-2'))
                self.assertIn('protocol', panel)
                self.assertIn('report_date', panel)
        luna = panels['gpt-6-luna', 'VulcanBench Frontier v4 retired-binary parity']
        sol = panels['gpt-6.1-sol', 'VulcanBench Frontier v4 retired-binary parity']
        self.assertEqual((luna['protocol'], luna['solver_cli'], luna['report_date']),
                         ('3.16', '0.155.0', '2026-09-28'))
        self.assertEqual((sol['protocol'], sol['solver_cli'], sol['report_date']),
                         ('3.18', '0.159.0', '2026-10-01'))
        self.assertEqual(luna['rows'][3], ['xhigh', 71.9354, 5.4728, 11, 23, 2, .159686, 21, False, 52.5745])
        self.assertEqual(sol['rows'][2], ['high', 88.2342, .4008, 23, 23, 0, .33007, 23, True, 10.2219])
        for model, expected in [('gpt-6-luna', ['max', 66.6, .22]), ('gpt-6.1-sol', ['high', 75.2, .65])]:
            panel = panels[model, 'DeepSWE v1.1 Score']
            self.assertEqual(panel['rows'][-1], expected)
            self.assertEqual(panel['columns'], ['effort', 'quality_percent', 'published_cost_per_task_usd'])
            for field in ['report_date', 'timing_seconds', 'sample_count', 'confidence_interval']:
                self.assertIsNone(panel[field])
        for field in ['ttft_seconds', 'generation_tokens_per_second']:
            self.assertIsNone(luna[field])
            self.assertIsNone(sol[field])
        prices = {p['model']: p for p in context['api_prices']}
        self.assertEqual(prices['gpt-6-luna']['input'], .1)
        self.assertEqual(prices['gpt-6.1-sol']['output'], 10)
        for price in prices.values():
            self.assertEqual(price['units'], 'USD per million tokens')
            self.assertIn('developers.openai.com', price['source_url'])
            self.assertIn('tool/cache-write charges excluded', price['service_tier'])
        self.assertTrue(all(v is None for v in context['local_accounting'].values()))
        wire = json.dumps(payload)
        for historical in ['local_observations', 'local_cumulative_tokens', 'preparation_id',
                           'source_sha256', 'HOST-CONFIG-OBSERVATION', 'jev-ccd70d9f',
                           'jev-d73ef7e0', 'jev-1f376b5d', 'jev-655c0169', '2511187']:
            self.assertNotIn(historical, wire)
        for rule in ['qualitative cross-model choice is allowed', 'model identity',
                     'protocol/date/harness confounds', 'No invalid pooled metric',
                     'combined rank', 'Pareto claim', 'not local acceptance probability']:
            self.assertIn(rule, context['comparison_rule'])
        instructions = payload['questions']['worker_pair']['instructions']
        for text in ['affordable useful accepted outcome', 'state.economic_context',
                     'concrete task requirement', 'research/review label', 'subscription quota']:
            self.assertIn(text, instructions)
        self.assertEqual(result['receipt']['request_sha256'],
                         __import__('hashlib').sha256(json.dumps(self.request, sort_keys=True).encode()).hexdigest())
        context['local_accounting']['provider_billing_usd'] = 0
        self.assertIsNone(jev_router.ECONOMIC_CONTEXT['local_accounting']['provider_billing_usd'])

    def test_economic_context_and_question_participate_in_source_binding(self):
        import shutil
        source = self.base / 'economic-source'
        shutil.copytree(BASE, source, ignore=shutil.ignore_patterns('.git', 'work', '__pycache__'))
        spec = importlib.util.spec_from_file_location('economic_adapter', source / 'runtime/adapter.py')
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        digest = adapter.source_digest(source)
        for relative, before, after in [
                ('runtime/factory/jev_router.py', b'CPI404_COST_CONTEXT_20261021_V1', b'CPI404_COST_CONTEXT_20261021_V2'),
                ('template/factory/assessment_questions.json', b'affordable', b'expensive')]:
            path = source / relative
            self.assertIn(path, adapter.source_files(source))
            original = path.read_bytes()
            self.assertIn(before, original)
            path.write_bytes(original.replace(before, after))
            self.assertNotEqual(adapter.source_digest(source), digest)
            path.write_bytes(original)
            self.assertEqual(adapter.source_digest(source), digest)

    def test_safe_actual_response_provenance_preserves_unknowns_and_redacts_credential(self):
        code, result, _ = self.command(transport=lambda p, *_: {
            **self.answer(p), '_http': {'status': 200, 'request_id': 'offline-request-id'},
            'status': 'completed'})
        self.assertEqual(code, 0)
        receipt = result['receipt']
        self.assertEqual(receipt['endpoint'], 'https://api.typesafe.ai/v1/systemone')
        self.assertEqual(receipt['returned_request_id'], 'offline-request-id')
        self.assertEqual(receipt['http_status'], 200)
        self.assertEqual(receipt['returned_status'], 'completed')
        self.assertEqual(receipt['usage'], {'input_tokens': 10, 'output_tokens': 4})
        self.assertIsNone(result['observed_effort'])
        self.assertNotIn('account', receipt)
        self.assertNotIn('offline-fixture-key', json.dumps(result))
        _, invalid, _ = self.command(transport=lambda p, *_: {
            **self.answer(p), '_http': {'status': True, 'request_id': 'offline-fixture-key'},
            'status': 'offline-fixture-key'})
        self.assertNotIn('returned_request_id', invalid['receipt'])
        self.assertNotIn('http_status', invalid['receipt'])
        self.assertNotIn('returned_status', invalid['receipt'])

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

    def test_explicit_pair_requires_actual_assessment_and_exact_constraint(self):
        self.request['explicit_choice'] = self.override()
        seen = []
        code, result, _ = self.command(transport=lambda p, *_: seen.append(p) or self.answer(p, effort='max'))
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'selected')
        self.assertEqual(result['receipt']['attempts'], 1)
        self.assertEqual(len(seen[0]['state']['allowed_catalog']), 8)
        self.assertIn('Human pair constraint', seen[0]['state']['task'])
        self.assertEqual(result['spawn_agent']['reasoning_effort'], 'max')
        code, result, _ = self.command()  # fixture selects medium, conflicting with max constraint
        self.assertEqual(code, 1)
        self.assertEqual(result['receipt']['reason'], 'human_constraint_not_selected')
        self.no_args(result)
        with patch.object(bridge, 'read_credential', return_value=None):
            code, result, _ = self.command(transport=lambda *_: self.fail('HTTP accessed'))
        self.assertEqual(code, 1)
        self.no_args(result)
        self.request['explicit_choice'] = self.override('gpt-6.1-sol', 'xhigh')
        self.assertEqual(self.command(transport=lambda *_: self.fail('HTTP accessed'))[0], 1)

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
        code, result, _ = self.command(transport=lambda p, *_: self.answer(p, effort='max'))
        self.assertEqual(code, 0)
        self.assertEqual(result['receipt']['assessment_status'], 'selected')
        self.assertEqual(result['receipt']['human_override'], {k: v for k, v in self.request['explicit_choice'].items() if k not in {'model', 'effort'}})

    def test_governing_override_requires_exact_quote_in_safe_bounded_readable_file(self):
        instruction = self.a / 'AGENTS.md'
        quote = self.override()['instruction_quote']
        self.request['explicit_choice'] = {
            **self.override(),
            'instruction_reference': {'kind': 'governing_instruction', 'path': str(instruction)}}

        def command():
            return self.command(transport=lambda p, *_: self.answer(p, effort='max'))

        instruction.write_text('Current governing instruction:\n' + quote + '\n')
        code, result, _ = command()
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'selected')
        self.assertEqual(result['receipt']['human_override']['instruction_quote'], quote)

        instruction.write_text('Different instruction. private-reference-must-not-leak')
        code, result, errors = command()
        self.assertEqual(code, 1)
        self.no_args(result)
        self.assertNotIn('private-reference-must-not-leak', json.dumps(result) + errors)

        instruction.write_text(quote)
        instruction.chmod(0)
        try:
            code, result, _ = command()
            self.assertEqual(code, 1)
            self.no_args(result)
        finally:
            instruction.chmod(0o644)

        instruction.write_text(quote + '\n' + 'x' * bridge.MAX_INSTRUCTION_REFERENCE)
        code, result, _ = command()
        self.assertEqual(code, 1)
        self.no_args(result)

        instruction.write_text(quote)
        instruction.chmod(0o666)
        try:
            code, result, _ = command()
            self.assertEqual(code, 1)
            self.no_args(result)
        finally:
            instruction.chmod(0o644)

        original = instruction.with_suffix('.original')
        instruction.rename(original)
        instruction.symlink_to(original)
        code, result, _ = command()
        self.assertEqual(code, 1)
        self.no_args(result)

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
                    code, result, _ = self.command(request, transport=lambda p, *_: self.answer(p, effort='max' if explicit else 'medium'))
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
        for path in ['work', '../escape', 'work/../work/other-task', str(self.b / 'work/outside')]:
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
            self.assertEqual(result.returncode, 1, result.stderr)
            output = json.loads(result.stdout)
            roots.append(output['project_binding']['root'])
            self.assertEqual(output['status'], 'refused')
            self.no_args(output)
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


class WorkerAdmissionTests(unittest.TestCase):
    setUp = RoutingTests.setUp
    project = RoutingTests.project
    bound_request = RoutingTests.bound_request
    answer = RoutingTests.answer
    override = RoutingTests.override
    no_args = RoutingTests.no_args

    def worker(self):
        request = copy.deepcopy(self.request)
        request['schema'] = 'CPI414PROVIDERWORKERV1'
        request.pop('capability_provenance')
        connection = '46edcd0d-a394-4516-a68e-214dc61ef7a0'
        captured = time.time()
        request['catalog_provenance'] = {'source': 'codex_app_server_same_connection',
                                        'connection_id': connection, 'captured_at': captured}
        request['catalog'] = {'same_account': True, 'identity': connection, 'fetched_at': captured,
            'models': [{'model': model, 'supportedReasoningEfforts': sorted(efforts)}
                       for model, efforts in jev_router.WORKER_EFFORTS.items()]}
        return request

    def command(self, request=None, reuse=None, transport=None, work='work/worker'):
        request = self.worker() if request is None else request
        args = ['route-worker', '--caller-id', 'native:fixture', '--work-dir', work]
        if reuse is not None:
            args += ['--reuse-receipt', reuse]
        output = io.StringIO()
        with (
            patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))),
            patch.object(consumer, 'project_root', return_value=self.a),
            patch.object(consumer, 'verify_source', return_value=self.a),
            patch.object(bridge, 'http_choice', side_effect=transport or (lambda p, *_: self.answer(p))),
            contextlib.redirect_stdout(output),
        ):
            with patch.object(sys, 'argv', ['factory', *args]):
                code = entry.main()
        return code, json.loads(output.getvalue())

    def prepared_native(self, caller_prefix='native:', work='work/native', carrier=False, project_policy=None):
        request = copy.deepcopy(self.request)
        if project_policy is not None:
            settings = json.loads((self.a / consumer.SETTINGS).read_text())
            settings['jev_router']['allowed_models'] = project_policy
            (self.a / consumer.SETTINGS).write_text(json.dumps(settings))
        session_id = '01a15564-fd5f-7550-aadb-de6160ef7b5d'
        caller_id = caller_prefix + session_id
        request['caller_id'] = caller_id
        request['capability_provenance']['caller_id'] = caller_id
        request['native_dispatch'] = {'task_name': 'parser_fix',
                                     'message': 'Repair the bounded parser and run its regression checks.'}
        code, prepared, _ = RoutingTests.command(self, request, work=work, caller_id=caller_id)
        self.assertEqual(code, 0)
        if not carrier:
            # Historical plaintext fixture only; a new receipt cannot downgrade.
            def historical(value):
                if isinstance(value, dict):
                    for key in ('native_request', 'native_task_name', 'native_transport'):
                        value.pop(key, None)
                    for child in value.values():
                        historical(child)
                elif isinstance(value, list):
                    for child in value:
                        historical(child)
            historical(prepared)
            Path(prepared['receipt_path']).write_text(json.dumps(prepared))
        message = request['native_dispatch']['message'] + '\n[CPI414 preparation=' + str(
            Path(prepared['receipt_path']).relative_to(self.a)) + ']'
        event = {'hook_event_name': 'PreToolUse', 'cwd': str(self.a),
                 'session_id': session_id, 'turn_id': 'turn:1', 'tool_use_id': 'tool:1',
                 'tool_name': 'spawn_agent', 'tool_input': {'task_name': 'parser_fix',
                 'message': message, 'model': 'gpt-6.1-sol', 'reasoning_effort': 'high',
                 'fork_turns': 'all'}}
        if carrier:
            event['tool_input'] = {**prepared['native_request'], 'message': 'opaque:v1:8ab09f'}
        return prepared, event

    def test_project_allowed_models_limits_actual_choice_and_preserves_luna_effort(self):
        settings = json.loads((self.a / consumer.SETTINGS).read_text())
        settings['jev_router']['allowed_models'] = ['gpt-6-luna']
        (self.a / consumer.SETTINGS).write_text(json.dumps(settings))
        for effort in ['low', 'medium', 'high', 'xhigh', 'max']:
            with self.subTest(effort=effort):
                request = copy.deepcopy(self.request)
                request['capability_provenance']['models'][0]['reasoning_efforts'] = [effort]
                seen = []
                code, result, _ = RoutingTests.command(
                    self, request, transport=lambda payload, *_: seen.append(payload) or self.answer(
                        payload, model='gpt-6-luna', effort=effort), work='work/policy-' + effort)
                self.assertEqual(code, 0)
                self.assertEqual(result['spawn_agent']['model'], 'gpt-6-luna')
                self.assertEqual(result['spawn_agent']['reasoning_effort'], effort)
                options = seen[0]['questions']['worker_pair']['criteria']
                self.assertEqual(len(options), 3)  # one pair plus the two refusal outcomes
                self.assertEqual(result['receipt']['assessment_capability_pairs'],
                                 [{'model': 'gpt-6-luna', 'effort': effort}])
                self.assertIn({'model': 'gpt-6.1-sol', 'effort': 'medium'},
                              result['receipt']['spawn_capability_pairs'])
                self.assertEqual(result['project_model_policy'], {'allowed_models': ['gpt-6-luna']})

    def test_project_policy_is_rechecked_at_launch_and_absent_policy_keeps_shared_choices(self):
        prepared, event = self.prepared_native(project_policy=['gpt-6-luna'])
        pair = prepared['spawn_agent']
        self.assertEqual(pair['model'], 'gpt-6-luna')
        observed = prepared['receipt']['spawn_capability_pairs']
        self.assertTrue(all({'model': 'gpt-6-luna', 'effort': effort} in observed
                            for effort in ['low', 'medium', 'high', 'xhigh', 'max']))
        self.assertIn({'model': 'gpt-6.1-sol', 'effort': 'high'}, observed)
        self.assertEqual(self.hook(event)['hookSpecificOutput']['permissionDecision'], 'allow')

        prepared, event = self.prepared_native(project_policy=['gpt-6-luna'], work='work/policy-tamper')
        settings = json.loads((self.a / consumer.SETTINGS).read_text())
        settings['jev_router']['allowed_models'] = ['gpt-6.1-sol']
        (self.a / consumer.SETTINGS).write_text(json.dumps(settings))
        self.assert_denied(self.hook(event))

        prepared, event = self.prepared_native(project_policy=['gpt-6-luna'], work='work/policy-pair-tamper')
        path = Path(prepared['receipt_path'])
        altered = copy.deepcopy(prepared)
        altered['spawn_agent']['model'] = 'gpt-6.1-sol'
        altered['spawn_agent']['reasoning_effort'] = 'medium'
        path.write_text(json.dumps(altered))
        self.assert_denied(self.hook(event))

        settings = json.loads((self.a / consumer.SETTINGS).read_text())
        settings['jev_router'].pop('allowed_models', None)
        (self.a / consumer.SETTINGS).write_text(json.dumps(settings))
        request = copy.deepcopy(self.request)
        seen = []
        code, result, _ = RoutingTests.command(
            self, request, transport=lambda payload, *_: seen.append(payload) or self.answer(
                payload, model='gpt-6.1-sol', effort='medium'), work='work/no-policy')
        self.assertEqual(code, 0)
        self.assertTrue(any('gpt-6.1-sol' in row for row in
                            [question for question in seen[0]['questions']['worker_pair']['criteria'].values()]))

    def test_malformed_project_model_policy_fails_before_credential_or_assessment(self):
        for invalid in [[], ['gpt-6-luna', 'gpt-6-luna'], ['gpt-5.6-sol'], 'gpt-6-luna', [None]]:
            with self.subTest(invalid=invalid):
                settings = json.loads((self.a / consumer.SETTINGS).read_text())
                settings['jev_router']['allowed_models'] = invalid
                (self.a / consumer.SETTINGS).write_text(json.dumps(settings))
                with patch.object(bridge, 'read_credential', side_effect=AssertionError('credential accessed')):
                    with self.assertRaises((ValueError, jev_router.ModelPolicyError)):
                        bridge.prepare_subagent(self.request, self.a, 'native:fixture', 'work/bad-policy',
                                                transport=lambda *_: self.fail('assessment called'))

    def hook(self, event):
        with patch.object(consumer, 'verify_source', return_value=self.a):
            return native_hook.handle(event, self.a)

    def assert_denied(self, result):
        output = result['hookSpecificOutput']
        self.assertEqual(output['permissionDecision'], 'deny')
        self.assertTrue(output['permissionDecisionReason'])
        self.assertNotIn('updatedInput', output)

    def test_provider_outer_api_selected_ordinary_and_human_are_private_persisted(self):
        for model, effort in [('gpt-6-luna', 'max'), ('gpt-6.1-sol', 'medium')]:
            code, result = self.command(transport=lambda p, *_: self.answer(p, model, effort))
            self.assertEqual(code, 0)
            self.assertEqual((result['model'], result['effort']), (model, effort))
            self.assertEqual(result['status'], 'confirmed_ordinary' if model == 'gpt-6.1-sol' else 'selected')
            self.assertEqual(json.loads(Path(result['receipt_path']).read_text()), result)
            self.assertEqual(Path(result['receipt_path']).stat().st_mode & 0o777, 0o600)
            self.assertNotIn('offline-fixture-key', json.dumps(result))
        request = self.worker()
        request['explicit_choice'] = self.override()
        request['human_attribution_verification'] = {
            'checked': True, 'checked_by': 'native:actual-caller', 'checked_at': '2026-10-20T12:00:00-05:00'}
        code, result = self.command(request, transport=lambda p, *_: self.answer(p, effort='max'))
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'selected')

    def test_provider_human_override_requires_caller_verification_and_persists_it(self):
        request = self.worker()
        request['explicit_choice'] = self.override()
        checked = {'checked': True, 'checked_by': 'native:actual-caller',
                   'checked_at': '2026-10-20T12:00:00-05:00'}
        invalid = [None, {}, {**checked, 'checked': False}, {**checked, 'checked': 1},
                   {**checked, 'checked_by': ' '}, {**checked, 'checked_by': ''},
                   {**checked, 'checked_at': 'not-a-timestamp'},
                   {**checked, 'checked_at': '2026-10-20'},
                   {**checked, 'checked_at': '2026-10-20T12:00:00'}]
        for verification in invalid:
            request['human_attribution_verification'] = verification
            with patch.object(bridge, 'read_credential', side_effect=AssertionError('credential accessed')):
                code, result = self.command(request, transport=lambda *_: self.fail('human override assessed'))
            self.assertEqual(code, 1, verification)
            self.no_args(result)
        request['human_attribution_verification'] = checked
        code, result = self.command(request, transport=lambda p, *_: self.answer(p, effort='max'))
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'selected')
        persisted = json.loads(Path(result['receipt_path']).read_text())
        self.assertEqual(persisted['receipt']['human_attribution_verification'], checked)
        self.assertEqual(persisted['human_attribution_verification'], checked)
        self.assertEqual(persisted['receipt']['assessment_status'], 'selected')

        request['continuation'] = True
        code, reused = self.command(request, result['receipt_path'],
                                    transport=lambda *_: self.fail('human continuation reassessed'))
        self.assertEqual(code, 0)
        self.assertEqual(reused['preparation_id'], result['preparation_id'])
        self.assertEqual(reused['receipt']['human_attribution_verification'], checked)
        request['human_attribution_verification'] = {**checked, 'checked_by': 'different-caller'}
        self.assertEqual(self.command(request, result['receipt_path'])[0], 1)

    def test_provider_all_admission_failures_have_zero_launch_ready_fields(self):
        modifications = [
            lambda r: r.update(project_binding={}),
            lambda r: r.update(caller_id='wrong'),
            lambda r: r.update(task_id=None),
            lambda r: r.update(task=''),
            lambda r: r['catalog'].update(same_account=False),
            lambda r: r['catalog_provenance'].update(connection_id='not-a-uuid'),
            lambda r: r['catalog_provenance'].update(captured_at=time.time()-70),
            lambda r: r['catalog'].update(identity='wrong-account'),
            lambda r: r['ordinary'].update(effort='xhigh'),
            lambda r: r.update(continuation=True),
            lambda r: r.update(remaining_ms=0),
            lambda r: r.update(explicit_choice={'model': 'gpt-6-luna', 'effort': 'low'}),
        ]
        for mutate in modifications:
            request = self.worker()
            mutate(request)
            code, result = self.command(request)
            self.assertEqual(code, 1, result)
            self.assertNotIn('model', result)
            self.assertNotIn('effort', result)
            self.assertNotIn('spawn_agent', result)
        for answer in ({}, {'answers': None}):
            code, result = self.command(transport=lambda *_: answer)
            self.assertEqual(code, 1)
            self.assertNotIn('model', result)
        with patch.object(bridge, 'persist_private', side_effect=OSError('fixture')):
            code, result = self.command()
        self.assertEqual(code, 1)
        self.assertEqual(result['persistence']['status'], 'failed')
        self.assertNotIn('model', result)

    def test_provider_persistence_failure_withholds_every_shared_route_launch_copy(self):
        # The shared route has real native and provider result variants. A
        # persistence failure cannot expose callable copies from either variant.
        for model, effort in [('gpt-6-luna', 'medium'), ('gpt-6.1-sol', 'medium')]:
            with self.subTest(model=model, effort=effort):
                routed = bridge.route(self.request, self.a, transport=lambda p, *_:
                                      self.answer(p, model=model, effort=effort))
                self.assertIn('spawn_agent', routed)
                self.assertIn('dispatch_receipt', routed)
                self.assertIn('spawn_agent_request', routed['receipt'])
                with patch.object(bridge, 'route', return_value=copy.deepcopy(routed)),                      patch.object(bridge, 'persist_private', side_effect=OSError('fixture persistence failure')):
                    code, result = self.command()
                self.assertEqual(code, 1)
                self.assertEqual(result['persistence']['status'], 'failed')
                self.no_args(result)
                self.assertEqual(result['receipt']['spawn_arguments'], 'unavailable')
                self.assertEqual(result['receipt']['prepared_pair'], {'model': model, 'effort': effort})

                def launch_copies(value):
                    if isinstance(value, dict):
                        return [key for key in value if key in
                                {'spawn_agent', 'dispatch_receipt', 'spawn_agent_request'}] + [
                                found for nested in value.values() for found in launch_copies(nested)]
                    if isinstance(value, list):
                        return [found for nested in value for found in launch_copies(nested)]
                    return []
                self.assertEqual(launch_copies(result), [])

    def test_provider_final_prompt_bound_is_not_truncated(self):
        request = self.worker()
        request['task'] = 'x' * 65536
        seen = []
        code, result = self.command(request, transport=lambda p, *_: seen.append(p) or self.answer(p))
        self.assertEqual(code, 0)
        self.assertEqual(seen[0]['state']['task'], request['task'])
        request['task'] += 'x'
        code, result = self.command(request)
        self.assertEqual(code, 1)
        self.assertNotIn('model', result)

    def test_provider_source_and_binding_checked_before_credential_access(self):
        request = self.worker()
        with (
            patch.object(consumer, 'verify_source', side_effect=ValueError('source drift')),
            patch.object(bridge, 'read_credential', side_effect=AssertionError('credential accessed')),
        ):
            with self.assertRaises(ValueError):
                bridge.prepare_worker(request, self.a, 'native:fixture', 'work/worker')

    def test_continuation_reuses_exact_original_and_rechecks_catalog_and_identity(self):
        code, first = self.command()
        self.assertEqual(code, 0)
        request = self.worker()
        request.update(continuation=True, task='Continue same bounded task with new verified facts.')
        code, reused = self.command(request, first['receipt_path'],
                                    transport=lambda *_: self.fail('continuation reassessed'))
        self.assertEqual(code, 0)
        self.assertEqual(reused['preparation_id'], first['preparation_id'])
        self.assertEqual(reused['model'], first['model'])
        self.assertEqual(reused['reuse'], 'same_logical_task')
        for mutate in [lambda r: r.update(task_id='different'),
                       lambda r: r['catalog']['models'][0].update(supportedReasoningEfforts=['low'])]:
            invalid = copy.deepcopy(request)
            mutate(invalid)
            code, result = self.command(invalid, first['receipt_path'])
            self.assertEqual(code, 1)
            self.assertNotIn('model', result)
        path = Path(first['receipt_path'])
        original = path.read_text()
        changed = json.loads(original)
        changed['source_binding']['revision'] = 'another-source'
        path.write_text(json.dumps(changed))
        self.assertEqual(self.command(request, str(path))[0], 1)
        path.write_text(original)
        path.chmod(0o644)
        self.assertEqual(self.command(request, str(path))[0], 1)


    def test_native_preparation_and_marker_share_legitimate_task_directory_names(self):
        for directory in ['work/réparation_日本語', 'work/parser+checks',
                          'work/parser (review)', 'work/修復 + (review) [checked]/nested']:
            for absolute in (False, True):
                with self.subTest(directory=directory, absolute=absolute):
                    supplied = str(self.a / directory) if absolute else directory
                    prepared, event = self.prepared_native(work=supplied)
                    path = Path(prepared['receipt_path'])
                    self.assertEqual(path.parent, self.a / directory)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    output = self.hook(event)['hookSpecificOutput']
                    self.assertEqual(output['permissionDecision'], 'allow')
                    applied = output['updatedInput']
                    self.assertEqual(applied, {**event['tool_input'], **prepared['spawn_agent']})
                    self.assert_denied(self.hook(event))
                    post = {**event, 'hook_event_name': 'PostToolUse', 'tool_input': applied,
                            'tool_response': {'agent_id': 'worker:unicode_path'}}
                    self.assertEqual(self.hook(post), {})
                    recorded = json.loads(path.with_suffix('.result.json').read_text())
                    self.assertEqual(recorded['native_handle'], 'worker:unicode_path')

    def test_provider_preparation_and_reuse_preserve_legitimate_task_directory_names(self):
        for directory in ['work/réparation_日本語', 'work/parser+checks',
                          'work/parser (review)', 'work/修復 + (review) [checked]/nested']:
            with self.subTest(directory=directory):
                code, prepared = self.command(work=directory)
                self.assertEqual(code, 0)
                self.assertEqual(Path(prepared['receipt_path']).parent, self.a / directory)
                request = self.worker()
                request['continuation'] = True
                locator = str(Path(prepared['receipt_path']).relative_to(self.a))
                code, reused = self.command(request, locator, work=directory,
                    transport=lambda *_: self.fail('same task reassessed'))
                self.assertEqual(code, 0)
                self.assertEqual(reused['preparation_id'], prepared['preparation_id'])

    def test_control_separator_workdirs_refuse_before_assessment_or_persistence(self):
        controls = ['\0', '\t', '\n', '\r', '\x1c', '\x1d', '\x1e', '\x1f',
                    '\x7f', '\x85', '\u2028', '\u2029', '\ud800']
        for char in controls:
            for absolute in (False, True):
                directory = 'work/control' + char + 'separator'
                supplied = str(self.a / directory) if absolute else directory
                for provider in (False, True):
                    with self.subTest(control=ascii(char), absolute=absolute, provider=provider):
                        with (patch.object(bridge, 'read_credential', return_value='offline-fixture-key') as key,
                              patch.object(bridge, 'http_choice') as chooser,
                              patch.object(bridge, 'persist_private') as writer):
                            if provider:
                                code, result = self.command(work=supplied, transport=chooser)
                                self.assertNotIn('model', result)
                                self.assertNotIn('effort', result)
                            else:
                                code, result, _ = RoutingTests.command(self, work=supplied, transport=chooser)
                                self.assertIsNone(result)
                            self.assertEqual(code, 1)
                            key.assert_not_called()
                            chooser.assert_not_called()
                            writer.assert_not_called()
                        self.assertFalse((self.a / 'work').exists())

    def test_native_marker_control_separators_refuse_before_receipt_read(self):
        _, event = self.prepared_native()
        for char in ['\0', '\t', '\n', '\r', '\x1e', '\x7f', '\x85', '\u2028', '\u2029', '\ud800']:
            with self.subTest(control=ascii(char)):
                invalid = copy.deepcopy(event)
                invalid['tool_input']['message'] = invalid['tool_input']['message'].replace(
                    'work/native/', 'work/native' + char + '/')
                with patch.object(native_hook, 'private_json') as reader:
                    self.assert_denied(self.hook(invalid))
                    reader.assert_not_called()


    def test_native_extended_marker_keeps_root_symlink_and_final_line_controls(self):
        prepared, event = self.prepared_native(work='work/修復 + (review)')
        path = Path(prepared['receipt_path'])
        link = self.a / 'work/link + (review)'
        link.symlink_to(path.parent, target_is_directory=True)
        locator = str(path.relative_to(self.a))
        for invalid_locator in [
                str(path), 'work/link + (review)/' + path.name,
                'work/../work/修復 + (review)/' + path.name,
                'work/../../b/work/修復 + (review)/' + path.name]:
            with self.subTest(locator=invalid_locator):
                invalid = copy.deepcopy(event)
                invalid['tool_input']['message'] = invalid['tool_input']['message'].replace(
                    locator, invalid_locator)
                self.assert_denied(self.hook(invalid))
        for suffix in ['\n', '\r\n', ' extra']:
            invalid = copy.deepcopy(event)
            invalid['tool_input']['message'] += suffix
            self.assert_denied(self.hook(invalid))
        self.assertFalse(path.with_suffix('.consume.json').exists())
        self.assertEqual(self.hook(event)['hookSpecificOutput']['permissionDecision'], 'allow')

    def test_native_exact_rewrite_once_and_actual_handle_receipt(self):
        prepared, event = self.prepared_native()
        for tool in ['spawn_agent', 'Agent']:
            with self.subTest(tool=tool):
                event['tool_name'] = tool
                result = self.hook(event)
                if tool == 'Agent':
                    self.assert_denied(result)  # same preparation cannot replay under alias
                    continue
                output = result['hookSpecificOutput']
                self.assertEqual(output['permissionDecision'], 'allow')
                applied = output['updatedInput']
                self.assertEqual(applied, {**event['tool_input'], **prepared['spawn_agent']})
                self.assertEqual(applied['fork_turns'], 'none')
        post = {**event, 'hook_event_name': 'PostToolUse',
                'tool_input': applied, 'tool_response': {'agent_id': 'worker:actual'}}
        self.assertEqual(self.hook(post), {})
        result_path = Path(prepared['receipt_path']).with_suffix('.result.json')
        result = json.loads(result_path.read_text())
        self.assertEqual(result['native_handle'], 'worker:actual')
        self.assertIsNone(result['observed_model'])
        self.assertEqual(result['acceptance'], 'not_performed')

    def test_native_post_conflicting_actual_pair_never_records_reported_handle(self):
        changes = [
            ('model', 'gpt-6.1-sol'), ('reasoning_effort', 'high'), ('fork_turns', 'all'),
            ('model', None), ('reasoning_effort', None), ('fork_turns', None)]
        for key, value in changes:
            with self.subTest(field=key, actual=value):
                prepared, event = self.prepared_native()
                applied = self.hook(event)['hookSpecificOutput']['updatedInput']
                path = Path(prepared['receipt_path'])
                reservation = path.with_suffix('.consume.json')
                original_reservation = reservation.read_bytes()
                actual = copy.deepcopy(applied)
                if value is None:
                    actual.pop(key)
                else:
                    actual[key] = value
                response = self.hook({**event, 'hook_event_name': 'PostToolUse',
                    'tool_input': actual, 'tool_response': {'agent_id': 'worker:must-not-be-recorded'}})
                self.assertFalse(path.with_suffix('.result.json').exists(),
                                 'conflicting actual arguments must not produce a handle receipt')
                self.assertIn('requires reconciliation',
                              response['hookSpecificOutput']['additionalContext'])
                self.assertEqual(reservation.read_bytes(), original_reservation)
                self.assertNotIn('worker:must-not-be-recorded', json.dumps(response))

    def test_native_post_checks_pair_even_when_reservation_hash_matches_conflicting_args(self):
        prepared, event = self.prepared_native()
        applied = self.hook(event)['hookSpecificOutput']['updatedInput']
        path = Path(prepared['receipt_path'])
        actual = {**applied, 'fork_turns': 'all'}
        reservation = path.with_suffix('.consume.json')
        saved = json.loads(reservation.read_text())
        saved['applied_arguments_sha256'] = __import__('hashlib').sha256(
            json.dumps(actual, sort_keys=True).encode()).hexdigest()
        reservation.write_text(json.dumps(saved))
        response = self.hook({**event, 'hook_event_name': 'PostToolUse',
                             'tool_input': actual, 'tool_response': {'agent_id': 'worker:wrong-pair'}})
        self.assertIn('requires reconciliation', response['hookSpecificOutput']['additionalContext'])
        self.assertFalse(path.with_suffix('.result.json').exists())

    def test_native_post_exact_args_still_require_original_reservation_hash(self):
        prepared, event = self.prepared_native()
        applied = self.hook(event)['hookSpecificOutput']['updatedInput']
        path = Path(prepared['receipt_path'])
        reservation = path.with_suffix('.consume.json')
        saved = json.loads(reservation.read_text())
        saved['applied_arguments_sha256'] = '0' * 64
        reservation.write_text(json.dumps(saved))
        response = self.hook({**event, 'hook_event_name': 'PostToolUse',
                             'tool_input': applied, 'tool_response': {'agent_id': 'worker:wrong-receipt'}})
        self.assertIn('requires reconciliation', response['hookSpecificOutput']['additionalContext'])
        self.assertFalse(path.with_suffix('.result.json').exists())

    def test_native_missing_wrong_task_session_root_expired_or_forbidden_denied(self):
        prepared, event = self.prepared_native()
        for mutate in [
            lambda e: e['tool_input'].update(message='unprepared'),
            lambda e: e['tool_input'].update(message=e['tool_input']['message'].replace('Repair', 'Change')),
            lambda e: e['tool_input'].update(task_name='another_task'),
            lambda e: e.update(session_id='wrong'),
            lambda e: e.update(cwd=str(self.b)),
            lambda e: e.pop('tool_use_id'),
            lambda e: e['tool_input'].update(preparation_id='unsupported'),
        ]:
            invalid = copy.deepcopy(event)
            mutate(invalid)
            self.assert_denied(self.hook(invalid))
        path = Path(prepared['receipt_path'])
        for mutate in [
            lambda r: r.update(capability_captured_at=time.time()-70),
            lambda r: r.update(native_dispatch=None),
            lambda r: r['spawn_agent'].update(reasoning_effort='ultra'),
            lambda r: r.update(project_binding={}),
            lambda r: r.update(source_binding={'revision': 'wrong', 'source': 'wrong-source'}),
            lambda r: r.update(status='refused'),
            lambda r: r.update(persistence={'status': 'failed'}),
        ]:
            receipt = copy.deepcopy(prepared)
            mutate(receipt)
            path.write_text(json.dumps(receipt))
            self.assert_denied(self.hook(event))
        self.assertFalse(path.with_suffix('.consume.json').exists())

    def test_native_private_path_symlink_and_internal_failure_denied(self):
        prepared, event = self.prepared_native()
        path = Path(prepared['receipt_path'])
        path.chmod(0o644)
        self.assert_denied(self.hook(event))
        path.chmod(0o600)
        renamed = path.with_suffix('.original')
        path.rename(renamed)
        path.symlink_to(renamed)
        self.assert_denied(self.hook(event))
        path.unlink()
        renamed.rename(path)
        with patch.object(native_hook, 'persist_private', side_effect=OSError('credential-like-secret')):
            result = self.hook(event)
        self.assert_denied(result)
        self.assertNotIn('credential-like-secret', json.dumps(result))
        self.assertFalse(path.with_suffix('.consume.json').exists())

    def test_actual_hook_entry_emits_supported_deny_on_malformed_input(self):
        result = subprocess.run([sys.executable, '-B', str(BASE / 'template/factory/native_hook.py'),
                                 '--root', str(self.a)], cwd=BASE, input='{bad',
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_denied(json.loads(result.stdout))

    def test_native_caller_accepts_only_raw_or_exact_native_prefix_uuid(self):
        for prefix in ['', 'native:']:
            prepared, event = self.prepared_native(caller_prefix=prefix)
            output = self.hook(event)['hookSpecificOutput']
            self.assertEqual(output['permissionDecision'], 'allow')
            consumed = json.loads(Path(prepared['receipt_path']).with_suffix('.consume.json').read_text())
            self.assertEqual(consumed['prepared_caller_id'], prepared['caller_id'])
            self.assertEqual(consumed['identity']['session_id'], event['session_id'])
            self.assertEqual(consumed['caller_mapping'],
                             'native_prefix_session_uuid' if prefix else 'exact_session_uuid')

        prepared, event = self.prepared_native()
        path = Path(prepared['receipt_path'])
        for caller in ['native:01a15564-fd5f-7550-aadb-de6160ef7b5e',
                       '01a15564-fd5f-7550-aadb-de6160ef7b5e',
                       'native:/root/real_system_implementation', '/root/real_system_implementation',
                       'native:native:' + event['session_id']]:
            modified = copy.deepcopy(prepared)
            modified['caller_id'] = caller
            path.write_text(json.dumps(modified))
            self.assert_denied(self.hook(event))
        path.write_text(json.dumps(prepared))
        for session in ['/root/real_system_implementation',
                        'native:' + event['session_id'], event['session_id'].upper()]:
            invalid = copy.deepcopy(event)
            invalid['session_id'] = session
            self.assert_denied(self.hook(invalid))
        modified = copy.deepcopy(prepared)
        modified['capability_provenance'] = 'unrelated_provider'
        path.write_text(json.dumps(modified))
        self.assert_denied(self.hook(event))
        self.assertFalse(path.with_suffix('.consume.json').exists())

    def test_shared_native_command_uses_actual_adopter_and_refuses_foreign_event_root(self):
        for root in [self.a, self.b]:
            self.a, original = root, self.a
            try:
                self.request = self.bound_request(root)
                prepared, event = self.prepared_native()
                output = io.StringIO()
                with patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(event).encode()))), \
                     patch.object(consumer, 'project_root', return_value=root), \
                     patch.object(consumer, 'verify_source', return_value=root), contextlib.redirect_stdout(output):
                    self.assertEqual(consumer.main(['native-hook']), 0)
                self.assertEqual(json.loads(output.getvalue())['hookSpecificOutput']['permissionDecision'], 'allow')
                event['cwd'] = str(self.b if root == original else original)
                output = io.StringIO()
                with patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(event).encode()))), \
                     patch.object(consumer, 'project_root', return_value=root), contextlib.redirect_stdout(output):
                    self.assertEqual(consumer.main(['native-hook']), 0)
                self.assert_denied(json.loads(output.getvalue()))
            finally:
                self.a = original

    def test_native_consumption_refuses_legacy_or_malformed_assessment_even_with_valid_pair(self):
        for mutate in [
            lambda r: r.update(status='human_override'),
            lambda r: r['receipt'].update(attempts=0),
            lambda r: r['receipt'].update(assessment_status='unassessed_human_override'),
            lambda r: r['receipt'].update(assessments={}),
            lambda r: r['receipt'].update(request_sha256='missing'),
            lambda r: r['receipt']['assessments']['worker_pair'].update(choice='uncertain'),
        ]:
            prepared, event = self.prepared_native()
            path = Path(prepared['receipt_path'])
            mutate(prepared)
            path.write_text(json.dumps(prepared))
            self.assert_denied(self.hook(event))
            self.assertFalse(path.with_suffix('.consume.json').exists())

    def test_same_task_continuation_cannot_reuse_legacy_unassessed_provider_receipt(self):
        request = self.worker()
        code, prepared = self.command(request)
        self.assertEqual(code, 0)
        path = Path(prepared['receipt_path'])
        prepared['receipt']['attempts'] = 0
        path.write_text(json.dumps(prepared))
        request['continuation'] = True
        code, refused = self.command(request, reuse=str(path), transport=lambda *_: self.fail('reassessed'))
        self.assertEqual(code, 1)
        self.no_args(refused)

    def test_native_alias_is_independently_rewritten(self):
        for tool in ['Agent', 'collaborationspawn_agent']:
            with self.subTest(tool=tool):
                _, event = self.prepared_native()
                event['tool_name'] = tool
                self.assertEqual(self.hook(event)['hookSpecificOutput']['permissionDecision'], 'allow')


class EncryptedNativePreparationTests(unittest.TestCase):
    setUp = RoutingTests.setUp
    project = RoutingTests.project
    bound_request = RoutingTests.bound_request
    answer = RoutingTests.answer
    prepared_native = WorkerAdmissionTests.prepared_native
    hook = WorkerAdmissionTests.hook
    assert_denied = WorkerAdmissionTests.assert_denied

    def test_complete_packet_persisted_and_opaque_pre_post_hashes_are_distinct(self):
        prepared, event = self.prepared_native(carrier=True, work='work/修復 + (review)')
        packet=prepared['native_request'];path=Path(prepared['receipt_path'])
        self.assertEqual(packet, {'task_name':prepared['native_task_name'],
            'message':'Repair the bounded parser and run its regression checks.', **prepared['spawn_agent']})
        self.assertRegex(packet['task_name'], r'^cpi414_[a-f0-9]+$')
        self.assertGreater(len(packet['task_name']),64) # source only; actual native acceptance remains unknown
        self.assertEqual(bridge.native_carrier_locator(packet['task_name']),path.relative_to(self.a).as_posix())
        self.assertEqual(json.loads(path.read_text()),prepared)
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        event['tool_input']['message']+='[CPI414 preparation=not_a_plaintext_locator]'
        applied=self.hook(event)['hookSpecificOutput']['updatedInput'];self.assertEqual(applied,event['tool_input'])
        record=json.loads(path.with_suffix('.consume.json').read_text())
        hash_text=lambda v:__import__('hashlib').sha256(v.encode()).hexdigest()
        self.assertEqual(record['declared_message_sha256'],hash_text(packet['message']))
        self.assertEqual(record['observed_message_sha256'],hash_text(applied['message']))
        self.assertNotEqual(record['declared_message_sha256'],record['observed_message_sha256'])
        self.assertEqual(record['plaintext_attestation'],'unavailable')
        self.assertNotIn(applied['message'],json.dumps(record))
        post={**event,'hook_event_name':'PostToolUse','tool_input':applied,'tool_response':{'agent_id':'worker:offline-observed'}}
        self.assertEqual(self.hook(post),{})
        result=json.loads(path.with_suffix('.result.json').read_text())
        self.assertEqual(result['native_handle'],'worker:offline-observed')
        self.assertEqual(result['observed_post_arguments_sha256'],record['applied_arguments_sha256'])
        self.assertEqual(result['observed_post_message_sha256'],record['observed_message_sha256'])
        self.assertEqual(result['plaintext_attestation'],'unavailable')
        self.assertIsNone(result['observed_effort']);self.assertEqual(result['acceptance'],'not_performed')

    def test_carrier_grammar_paths_symlinks_and_private_files_refuse(self):
        prepared,event=self.prepared_native(carrier=True);path=Path(prepared['receipt_path']);locator=path.relative_to(self.a).as_posix()
        bad=['cpi414_','cpi414_AA','cpi414_0','cpi414_zz','cpi414_ff','cpi414_'+locator.encode().hex().upper()]
        for value in [str(path),'work/../'+locator,'work//native/'+path.name,'work/../../b/'+locator,
                      'other/native/'+path.name,'work/native/jev-'+'0'*32+'.json',
                      'work/native/../native/'+path.name,'work/native/jev-'+'1'*32+'.json\n']:
            bad.append('cpi414_'+value.encode().hex())
        link=self.a/'work/link';link.symlink_to(path.parent,target_is_directory=True)
        bad.append('cpi414_'+('work/link/'+path.name).encode().hex())
        for name in bad:
            invalid=copy.deepcopy(event);invalid['tool_input']['task_name']=name;self.assert_denied(self.hook(invalid))
        hardlink=path.with_suffix('.hardlink');os.link(path,hardlink)
        self.assert_denied(self.hook(event));hardlink.unlink()
        path.chmod(0o644);self.assert_denied(self.hook(event));path.chmod(0o600)
        original=path.with_suffix('.original');path.rename(original);path.symlink_to(original)
        self.assert_denied(self.hook(event));path.unlink();original.rename(path)
        self.assertFalse(path.with_suffix('.consume.json').exists())

    def test_declared_task_transport_and_complete_packet_tampering_refuse(self):
        prepared,event=self.prepared_native(carrier=True);path=Path(prepared['receipt_path'])
        for mutate in [lambda r:r['native_dispatch'].update(task_name='another_task'),
                       lambda r:r['native_dispatch'].update(message_sha256='0'*64),
                       lambda r:r['native_transport'].update(schema='UNKNOWN'),
                       lambda r:r['native_transport'].update(receipt_locator='work/foreign/receipt.json'),
                       lambda r:r.update(native_task_name='another_task'),
                       lambda r:r['native_request'].update(message='Different plaintext task'),
                       lambda r:r['native_request'].update(model='gpt-6.1-sol'),
                       lambda r:r['native_request'].update(extra='unsupported')]:
            modified=copy.deepcopy(prepared);mutate(modified);path.write_text(json.dumps(modified));self.assert_denied(self.hook(event))
        self.assertFalse(path.with_suffix('.consume.json').exists())

    def test_caller_project_context_source_expiry_assessment_guards_remain(self):
        prepared,event=self.prepared_native(carrier=True);path=Path(prepared['receipt_path'])
        for mutate in [lambda e:e.update(session_id='foreign'),lambda e:e.update(cwd=str(self.b)),
                       lambda e:e.pop('tool_use_id'),lambda e:e['tool_input'].update(extra='unsupported'),
                       lambda e:e['tool_input'].update(message='')]:
            invalid=copy.deepcopy(event);mutate(invalid);self.assert_denied(self.hook(invalid))
        for mutate in [lambda r:r.update(caller_id='native:foreign'),lambda r:r.update(task_id=''),
                       lambda r:r.update(project_binding=consumer.project_binding(self.b)),lambda r:r.update(source_binding={}),
                       lambda r:r.update(capability_captured_at=time.time()-70),
                       lambda r:r.update(status='refused'),lambda r:r.update(persistence={'status':'failed'}),
                       lambda r:r['receipt'].update(attempts=0),lambda r:r['receipt'].update(assessments={}),
                       lambda r:r['spawn_agent'].update(reasoning_effort='ultra')]:
            modified=copy.deepcopy(prepared);mutate(modified);path.write_text(json.dumps(modified));self.assert_denied(self.hook(event))
        path.write_text(json.dumps(prepared));(self.a/'.factory/project-context.md').write_text('Changed actual context')
        self.assert_denied(self.hook(event));self.assertFalse(path.with_suffix('.consume.json').exists())

    def test_exact_carrier_pair_fork_packet_and_single_reservation(self):
        prepared,event=self.prepared_native(carrier=True);path=Path(prepared['receipt_path'])
        for key,value in [('model','gpt-6.1-sol'),('reasoning_effort','ultra'),('fork_turns','all')]:
            invalid=copy.deepcopy(event);invalid['tool_input'][key]=value;result=self.hook(invalid);self.assert_denied(result)
            self.assertIn('native_packet_pair_mismatch',json.dumps(result))
        self.assertEqual(self.hook(event)['hookSpecificOutput']['updatedInput'],event['tool_input'])
        result=self.hook(event);self.assert_denied(result);self.assertIn('native_dispatch_already_reserved',json.dumps(result))
        self.assertTrue(path.with_suffix('.consume.json').exists())

    def test_failed_persistence_withholds_every_callable_packet_copy(self):
        caller='native:01a15564-fd5f-7550-aadb-de6160ef7b5d';request=copy.deepcopy(self.request)
        request['caller_id']=caller;request['capability_provenance']['caller_id']=caller
        request['native_dispatch']={'task_name':'parser_fix','message':'Run the exact bounded parser repair.'}
        with patch.object(bridge.os,'fsync',side_effect=OSError('secret must not escape')):
            code,result,errors=RoutingTests.command(self,request,caller_id=caller)
        self.assertEqual(code,1);self.assertEqual(result['status'],'refused');self.assertEqual(result['persistence']['status'],'failed')
        for box in [result,result['receipt']]:
            for key in ['spawn_agent','native_request','native_task_name','native_transport','dispatch_receipt','spawn_agent_request']:
                self.assertNotIn(key,box)
        self.assertNotIn('secret must not escape',json.dumps(result)+errors)

    def test_post_opaque_identity_pair_refuse_but_exact_post_expiry_accepts(self):
        prepared,event=self.prepared_native(carrier=True);path=Path(prepared['receipt_path'])
        applied=self.hook(event)['hookSpecificOutput']['updatedInput']
        post={**event,'hook_event_name':'PostToolUse','tool_input':applied,'tool_response':{'agent_id':'worker:observed'}}
        reserved=path.with_suffix('.consume.json').read_bytes()
        for mutate in [lambda e:e['tool_input'].update(message='changed opaque ciphertext'),
                       lambda e:e['tool_input'].update(model='gpt-6.1-sol'),
                       lambda e:e['tool_input'].update(fork_turns='all'),lambda e:e.update(turn_id='turn:foreign')]:
            invalid=copy.deepcopy(post);mutate(invalid);result=self.hook(invalid)
            self.assertIn('requires reconciliation',result['hookSpecificOutput']['additionalContext'])
            self.assertFalse(path.with_suffix('.result.json').exists());self.assertEqual(path.with_suffix('.consume.json').read_bytes(),reserved)
        with patch.object(native_hook.time,'time',return_value=prepared['capability_expires_at']+10):
            self.assert_denied(self.hook(event));self.assertEqual(self.hook(post),{})
        self.assertEqual(json.loads(path.with_suffix('.result.json').read_text())['native_handle'],'worker:observed')

    def test_unknown_handle_and_safe_stable_refusal_diagnostics(self):
        prepared,event=self.prepared_native(carrier=True);path=Path(prepared['receipt_path'])
        applied=self.hook(event)['hookSpecificOutput']['updatedInput']
        post={**event,'hook_event_name':'PostToolUse','tool_input':applied,'tool_response':{'task_name':'/root/unknown-native-handle'}}
        self.assertEqual(self.hook(post),{});result=json.loads(path.with_suffix('.result.json').read_text())
        self.assertEqual(result['status'],'unknown_native_result');self.assertIsNone(result['native_handle'])
        self.assertIn('native_dispatch_already_reserved',json.dumps(self.hook(post)))
        prepared,event=self.prepared_native(carrier=True)
        with patch.object(native_hook,'persist_private',side_effect=OSError('private credential-like value')):result=self.hook(event)
        self.assert_denied(result);self.assertIn('native_private_io_refused',json.dumps(result));self.assertNotIn('private credential-like value',json.dumps(result))

    def test_legacy_plaintext_only_and_new_transport_never_downgrades(self):
        prepared,event=self.prepared_native();opaque=copy.deepcopy(event);opaque['tool_input']['message']='opaque:v1:ciphertext'
        self.assert_denied(self.hook(opaque));self.assertEqual(self.hook(event)['hookSpecificOutput']['permissionDecision'],'allow')
        prepared,event=self.prepared_native(carrier=True);event['tool_input']['task_name']=prepared['native_dispatch']['task_name']
        event['tool_input']['message']=prepared['native_request']['message']+'\n[CPI414 preparation='+str(Path(prepared['receipt_path']).relative_to(self.a))+']'
        result=self.hook(event);self.assert_denied(result);self.assertIn('native_transport_mode_mismatch',json.dumps(result))

    def test_copied_generated_adapters_reach_same_successor_without_launch(self):
        import shutil
        source=self.base/'copied-successor';shutil.copytree(BASE,source,ignore=shutil.ignore_patterns('.git','work','__pycache__'))
        for args in [['init','-q'],['config','user.name','Offline fixture'],['config','user.email','fixture@example.invalid'],['add','runtime','template'],['commit','-qm','offline adapter fixture']]:
            subprocess.run(['git',*args],cwd=source,check=True,capture_output=True)
        spec=importlib.util.spec_from_file_location('offline_adapter_smoke',source/'runtime/adapter.py')
        adapters=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapters)
        bound=adapters.binding(source,consumer.project_binding(self.a))
        for name in ['jev_prepared_bridge','native_hook']:
            path=self.a/'factory'/(name+'.py');path.parent.mkdir(exist_ok=True)
            path.write_bytes(adapters.render('factory/'+name+'.py',bound))
            module_spec=importlib.util.spec_from_file_location('offline_installed_'+name,path)
            module=importlib.util.module_from_spec(module_spec);sys.modules[module_spec.name]=module;module_spec.loader.exec_module(module)
            target=sys.modules[module_spec.name];self.assertEqual(target.SHARED_SOURCE,source)
            if name=='jev_prepared_bridge':
                locator='work/task/jev-'+'1'*32+'.json';self.assertEqual(target.native_carrier_locator(target.native_carrier(locator)),locator)
            else:self.assertIn('native_transport_binding_mismatch',target.REFUSAL_REASONS)
        self.assertEqual(adapters.source_digest(source),bound['digest'])

class NativeTaskNameResultTests(unittest.TestCase):
    setUp = RoutingTests.setUp
    project = RoutingTests.project
    bound_request = RoutingTests.bound_request
    answer = RoutingTests.answer
    prepared_native = WorkerAdmissionTests.prepared_native
    hook = WorkerAdmissionTests.hook
    assert_denied = WorkerAdmissionTests.assert_denied

    def post_result(self, response, *, carrier=True):
        prepared, event = self.prepared_native(carrier=carrier)
        path = Path(prepared['receipt_path'])
        applied = self.hook(event)['hookSpecificOutput']['updatedInput']
        post = {**event, 'hook_event_name': 'PostToolUse',
                'tool_input': applied, 'tool_response': response}
        self.assertEqual(self.hook(post), {})
        return prepared, event, post, json.loads(path.with_suffix('.result.json').read_text())

    def test_canonical_carrier_result_object_and_json_preserve_unknown_usage(self):
        for encoded in (False, True):
            with self.subTest(encoded=encoded):
                prepared, event = self.prepared_native(carrier=True)
                path = Path(prepared['receipt_path'])
                applied = self.hook(event)['hookSpecificOutput']['updatedInput']
                canonical = '/root/' + applied['task_name']
                self.assertFalse(bridge._identifier(canonical))
                response = {'task_name': canonical, 'nickname': None}
                post = {**event, 'hook_event_name': 'PostToolUse', 'tool_input': applied,
                        'tool_response': json.dumps(response) if encoded else response}
                self.assertEqual(self.hook(post), {})
                result = json.loads(path.with_suffix('.result.json').read_text())
                self.assertEqual(result['status'], 'native_handle_observed')
                self.assertEqual(result['native_handle'], canonical)
                self.assertEqual(result['declared_task'], prepared['native_dispatch'])
                reserved = json.loads(path.with_suffix('.consume.json').read_text())
                self.assertEqual(result['observed_post_arguments_sha256'], reserved['applied_arguments_sha256'])
                self.assertEqual(result['observed_post_message_sha256'], reserved['observed_message_sha256'])
                for key in ('observed_model', 'observed_effort', 'worker_usage'):
                    self.assertIsNone(result[key])
                self.assertEqual(result['acceptance'], 'not_performed')
                self.assertEqual(result['plaintext_attestation'], 'unavailable')
                self.assertIn('native_dispatch_already_reserved', json.dumps(self.hook(post)))

    def test_canonical_path_grammar_without_generic_identifier_expansion(self):
        for value in ('/root/task_3', '/root/task1/task_3', '/root/' + 'a' * 1024):
            self.assertEqual(native_hook.native_result_handle({'task_name': value}), value)
            self.assertFalse(bridge._identifier(value))
        for value in (None, 3, '', '/root', '/root/', 'task_3', 'root/task_3', '/other/task_3',
                      '/root//task_3', '/root/task-3', '/root/Task3', '/root/task.3',
                      '/root/../task_3', '/root/task_3/', '/root/task_3\n', '/root/修復'):
            with self.subTest(value=value):
                self.assertIsNone(native_hook.native_result_handle({'task_name': value}))

    def test_legacy_ids_and_nullable_fallback_keep_plaintext_and_carrier_results(self):
        for carrier in (False, True):
            for response in ({'agent_id': 'worker:legacy'}, {'agentId': 'worker:legacy'},
                             {'agent_id': None, 'agentId': 'worker:legacy'},
                             {'agent_id': 'worker:legacy', 'agentId': 'worker:legacy'}):
                with self.subTest(carrier=carrier, response=response):
                    _, _, _, result = self.post_result(response, carrier=carrier)
                    self.assertEqual(result['status'], 'native_handle_observed')
                    self.assertEqual(result['native_handle'], 'worker:legacy')
        self.assertIsNone(native_hook.native_result_handle({'agent_id': '/root/task_3'}))

    def test_absent_malformed_error_and_conflicting_results_stay_unknown(self):
        responses = [None, [], 'null', {}, {'nickname': 'only metadata'},
                     {'task_name': '/root/invalid-name'}, {'agent_id': ['worker:a']},
                     {'agent_id': 'worker:a', 'agentId': 'worker:b'},
                     {'task_name': '/root/task_3', 'agent_id': 'worker:a'},
                     {'task_name': '/root/task_3', 'agentId': 'worker:a'},
                     {'task_name': None, 'agent_id': 'worker:a'},
                     {'task_name': '/root/task_3', 'error': 'failure must not be success'},
                     {'agent_id': 'worker:a', 'isError': True},
                     {'agentId': 'worker:a', 'success': False},
                     {'content': [{'task_name': '/root/task_3'}]}]
        for response in responses:
            with self.subTest(response=response):
                _, _, _, result = self.post_result(response)
                self.assertEqual(result['status'], 'unknown_native_result')
                self.assertIsNone(result['native_handle'])
                self.assertNotIn('failure must not be success', json.dumps(result))

    def test_malformed_json_response_requires_reconciliation_without_result(self):
        prepared, event = self.prepared_native(carrier=True)
        path = Path(prepared['receipt_path'])
        applied = self.hook(event)['hookSpecificOutput']['updatedInput']
        post = {**event, 'hook_event_name': 'PostToolUse', 'tool_input': applied,
                'tool_response': '{malformed private-like output'}
        result = self.hook(post)
        self.assertIn('requires reconciliation', result['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('private-like', json.dumps(result))
        self.assertFalse(path.with_suffix('.result.json').exists())
        self.assertTrue(path.with_suffix('.consume.json').exists())

    def test_canonical_result_preserves_exact_opaque_and_identity_post_expiry_guards(self):
        prepared, event = self.prepared_native(carrier=True)
        path = Path(prepared['receipt_path'])
        applied = self.hook(event)['hookSpecificOutput']['updatedInput']
        reserved = path.with_suffix('.consume.json').read_bytes()
        canonical = '/root/' + applied['task_name']
        post = {**event, 'hook_event_name': 'PostToolUse', 'tool_input': applied,
                'tool_response': {'task_name': canonical}}
        changes = [('tool_input', 'message', 'opaque:v1:changed'),
                   ('tool_input', 'task_name', 'cpi414_00'),
                   ('tool_input', 'model', 'gpt-6.1-sol'),
                   ('tool_input', 'reasoning_effort', 'ultra'),
                   ('tool_input', 'fork_turns', 'all'),
                   (None, 'session_id', '01a15564-fd5f-7550-aadb-de6160ef7b5e'),
                   (None, 'turn_id', 'other-turn'), (None, 'tool_use_id', 'other-tool')]
        for box, key, value in changes:
            with self.subTest(key=key):
                invalid = copy.deepcopy(post)
                (invalid[box] if box else invalid)[key] = value
                self.assertIn('requires reconciliation', self.hook(invalid)['hookSpecificOutput']['additionalContext'])
                self.assertFalse(path.with_suffix('.result.json').exists())
                self.assertEqual(path.with_suffix('.consume.json').read_bytes(), reserved)
        with patch.object(native_hook.time, 'time', return_value=prepared['capability_expires_at'] + 10):
            self.assert_denied(self.hook(event))
            self.assertEqual(self.hook(post), {})
        result = json.loads(path.with_suffix('.result.json').read_text())
        self.assertEqual(result['native_handle'], canonical)
        self.assertEqual(result['observed_post_arguments_sha256'],
                         json.loads(reserved)['applied_arguments_sha256'])


if __name__ == '__main__':
    unittest.main()
