"""Changed-boundary coverage for project-neutral caller bindings."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[2]
CALLER_PATH = Path(__file__).with_name('project_caller.py').resolve()
ADAPTIVE_ROOT = REPOSITORY / 'tools/project-task-runner/adaptive'
SPEC = importlib.util.spec_from_file_location('project_caller_boundary_test', CALLER_PATH)
caller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(caller)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write_private(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(raw)
    path.chmod(0o600)


def write_document(path, document):
    raw = json.dumps(document, sort_keys=True, separators=(',', ':')).encode() + b'\n'
    write_private(path, raw)
    return raw


def candidate_pins():
    files = [REPOSITORY / 'tools/project-task-runner/runner.py',
             REPOSITORY / 'scripts/project-task-runner',
             Path(sys.executable).resolve(strict=True),
             ADAPTIVE_ROOT / 'policy.py', ADAPTIVE_ROOT / 'retention.py',
             ADAPTIVE_ROOT / 'runner.py']
    return {str(path): digest(path.read_bytes()) for path in files}


def packet_documents(root, state, pins):
    """Build a minimal inert Root-shaped packet chain under this test project."""
    packet_root = root / 'work/packets'
    packet_root.mkdir(parents=True, mode=0o700)
    evidence_path = packet_root / 'SOURCE-EVIDENCE-INERT.json'
    candidate_path = packet_root / 'CANDIDATE-INERT.json'
    adoption_path = packet_root / 'ADOPTION-INERT.json'
    observed_merge = '0' * 40
    source = {'observed_merge': observed_merge}
    evidence = {'source': source, 'runtime_source_pins': pins}
    evidence_raw = write_document(evidence_path, evidence)
    evidence_ref = {'path': str(evidence_path), 'sha256': digest(evidence_raw)}
    accounting_reader_path = REPOSITORY / 'accounting/capacity_budget.py'
    capacity_guard_path = REPOSITORY / 'accounting/capacity_guard.py'
    accounting_reader = {'path': str(accounting_reader_path),
                         'sha256': digest(accounting_reader_path.read_bytes())}
    capacity_guard = {'path': str(capacity_guard_path),
                      'sha256': digest(capacity_guard_path.read_bytes())}
    candidate = {
        'project_root': str(root), 'canonical_state': str(state),
        'source_evidence': evidence_ref, 'runtime_source_pins': pins,
        'accounting_reader': accounting_reader, 'capacity_guard': capacity_guard,
    }
    candidate_raw = write_document(candidate_path, candidate)
    candidate_ref = {'path': str(candidate_path), 'sha256': digest(candidate_raw)}
    adoption = {
        'project_root': str(root), 'canonical_state': str(state),
        'code_root': str(REPOSITORY),
        'runtime_path': str(ADAPTIVE_ROOT),
        'state': 'INERT_ROOT_PACKET_FIXTURE',
        'actual_job_authorized': False,
        'actual_runtime_adoption_authorized': True,
        'candidate': str(candidate_path), 'candidate_sha256': candidate_ref['sha256'],
        'source_validation': {'root_source_evidence': evidence_ref},
        'runtime_source_pins': pins,
        'accounting_reader': accounting_reader, 'capacity_guard': capacity_guard,
    }
    adoption_raw = write_document(adoption_path, adoption)
    descriptors = {
        'source_evidence': {'path': str(evidence_path.relative_to(root)),
                            'sha256': digest(evidence_raw)},
        'candidate': {'path': str(candidate_path.relative_to(root)),
                      'sha256': digest(candidate_raw)},
        'adoption': {'path': str(adoption_path.relative_to(root)),
                     'sha256': digest(adoption_raw)},
    }
    return descriptors, observed_merge


def make_binding(work, *, project_id='inert-example-project', packets=False):
    root = work / 'inert-project'
    state = root / 'state'
    (root / '.factory').mkdir(parents=True)
    (root / 'work').mkdir(parents=True, mode=0o700)
    (root / 'work/jobs').mkdir(mode=0o700)
    (root / 'work').chmod(0o700)
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    (root / 'AGENTS.md').write_text('inert fixture\n', encoding='utf-8')
    (root / '.factory/project-context.md').write_text('inert fixture\n', encoding='utf-8')
    pins = candidate_pins()

    # A tiny test-local protocol shim exercises only explicit generic APIs.
    context_path = root / 'project_context.py'
    context_source = b'''from pathlib import Path\n\ndef read_root_packets(project_root, *, contract, caller_api, context_api):\n    root = Path(project_root)\n    packets, refs = context_api.read_pinned_packet_set(contract["packets"], root, caller_api)\n    context_api.validate_root_packet_chain(packets, refs, contract, root)\n    return packets, refs\n\ndef bind_runtime(project_root=None, *, caller_api, project_id, packets, references, contract, context_api):\n    return context_api.bind_root_runtime_context(\n        packets, references, contract, Path(project_root), project_id, caller_api, context_api)\n'''
    write_private(context_path, context_source)

    contract = {
        'actual_job_authorized': False,
        'actual_runtime_adoption_authorized': True,
        'project_root': str(root), 'canonical_state': str(state),
        'code_root': str(REPOSITORY), 'runtime_path': str(ADAPTIVE_ROOT),
        'observed_merge': '0' * 40,
        'adoption_state': 'INERT_ROOT_PACKET_FIXTURE',
        'runtime_module_name': 'inert_adaptive_runtime',
        'legacy_project_binding': {
            'project_id': project_id, 'unit_prefix': 'inert-task',
            'payload_environment': {'INERT_TASK_CONTROL': '{job}/control.json'}},
        'packets': {},
    }
    if packets:
        descriptors, observed_merge = packet_documents(root, state, pins)
        contract['packets'] = descriptors
        contract['observed_merge'] = observed_merge

    caller_contract = {
        'binding_schema': 'generic-project-caller-binding-v1',
        'runtime_context_sha256': '0' * 64,
        'source_view_sha256': '0' * 64,
        'selection_schema': 'INERT_SELECTION_V1',
        'successor_profile_schema': 'INERT_SUCCESSOR_PROFILE_V1',
        'legacy_profile_schema': 'INERT_PROFILE_V1',
        'source_provenance_schema': 'INERT_SOURCE_MAP_V1',
        'receipt_schema': 'INERT_RECEIPT_V1',
        'qualification_source_names': ['caller.py'],
        'historical_source_names': ['caller.py', 'runtime_context.py'],
        'caller_entry_name': 'caller.py', 'runtime_context_name': 'runtime_context.py',
        'source_view_name': 'source_view.py', 'profile_names': ['profile.json'],
        'promotion_name': 'promotion.json', 'receipt_name': 'receipt.json',
    }
    binding = {
        'schema': caller_contract['binding_schema'], 'project_id': project_id,
        'project_root': str(root), 'tooling_commit': '0' * 40,
        'tooling_main_commit': '0' * 40,
        'generic_runner_path': str(REPOSITORY / 'tools/project-task-runner/runner.py'),
        'generic_runner_sha256': pins[str(REPOSITORY / 'tools/project-task-runner/runner.py')],
        'runtime_path': str(ADAPTIVE_ROOT),
        'runtime_runner_sha256': pins[str(ADAPTIVE_ROOT / 'runner.py')],
        'canonical_state': str(state), 'code_root': str(REPOSITORY),
        'qualification_root': 'work/qualification',
        'project_context_path': str(context_path),
        'project_context_sha256': digest(context_source),
        'caller_api_path': str(REPOSITORY / 'tools/project-task-runner/caller_api.py'),
        'caller_api_sha256': digest((REPOSITORY / 'tools/project-task-runner/caller_api.py').read_bytes()),
        'caller_runtime_path': str(REPOSITORY / 'tools/project-task-runner/caller_runtime.py'),
        'caller_runtime_sha256': digest((REPOSITORY / 'tools/project-task-runner/caller_runtime.py').read_bytes()),
        'candidate_adaptive_root': str(ADAPTIVE_ROOT), 'candidate_source_pins': pins,
        'candidate_context_api_sha256': digest(
            (REPOSITORY / 'tools/project-task-runner/project_context.py').read_bytes()),
        'caller_path': str(CALLER_PATH), 'caller_sha256': digest(CALLER_PATH.read_bytes()),
        'tooling_candidate_root': str(REPOSITORY),
        'project_runtime_binding': contract['legacy_project_binding'],
        'root_packet_contract': contract, 'caller_contract': caller_contract,
    }
    binding_path = work / 'binding.json'
    write_document(binding_path, binding)
    return binding, binding_path, root, state


class FakeRuntime:
    class Refused(Exception):
        pass

    def __init__(self, config, job):
        self.config = config
        self.job = Path(job)
        self.calls = []

    def configuration(self, path):
        self.calls.append(('configuration', Path(path)))
        return self.config, 'configuration-digest'

    def launch(self, job, config):
        self.calls.append(('launch', Path(job), Path(config)))
        return {'state': 'MOCK-LAUNCH', 'job': str(job)}

    def load_job(self, job):
        self.calls.append(('load_job', Path(job)))
        return Path(job), {'contract_version': 1, 'config': self.config}

    def public_status(self, job):
        self.calls.append(('public_status', Path(job)))
        return {'state': 'MOCK-STATUS'}

    def stop(self, job):
        self.calls.append(('stop', Path(job)))
        return {'state': 'MOCK-STOP'}


def successor_profile_fixture(directory):
    root = Path(directory) / 'profile-project'
    work = root / 'work'
    qualification = work / 'qualification'
    state = root / 'state'
    jobs = work / 'jobs'
    for path in (qualification, state, jobs, root / 'vendor'):
        path.mkdir(parents=True, exist_ok=True)
    job = jobs / 'future'
    config_path = work / 'config.json'
    config_path.write_text('{}\n', encoding='utf-8')
    executable = Path(sys.executable).resolve(strict=True)
    api_path = root / 'vendor/caller_api.py'
    api_path.write_text('# inert API source pin\n', encoding='utf-8')
    tooling_root = root / 'vendor/tooling'
    helper_root = tooling_root / 'tools/project-task-runner'
    helper_root.mkdir(parents=True)
    caller_runtime_path = helper_root / 'caller_runtime.py'
    caller_runtime_path.write_text('# inert runtime API source pin\n', encoding='utf-8')
    context_api_path = helper_root / 'project_context.py'
    context_api_path.write_text('# inert context API source pin\n', encoding='utf-8')
    context_path = work / 'project-context.json'
    context_path.write_text('{}\n', encoding='utf-8')
    api_sha = digest(api_path.read_bytes())
    context_sha = digest(context_path.read_bytes())
    tooling_commit = '1' * 40
    pins = {str(executable): digest(executable.read_bytes())}
    accounting = {
        'accounting_reader': {'path': '/tooling/accounting/reader.py', 'sha256': '2' * 64},
        'capacity_guard': {'path': '/tooling/accounting/guard.py', 'sha256': '3' * 64},
    }
    selection = {name: {'path': str(work / 'packets' / f'{name}.json'),
                        'sha256': str(index) * 64}
                 for index, name in enumerate(('source_evidence', 'candidate', 'adoption'), 1)}
    bound = {
        'project_root': root, 'canonical_state': state,
        'context_selection': {'schema': 'root-successor-05', **selection},
        'adoption': {'actual_job_authorized': True, 'code_root': str(tooling_root),
                     'runtime_path': str(tooling_root / 'tools/project-task-runner/adaptive')},
        'source_evidence': {'source': {'observed_merge': '4' * 40}},
        'candidate': {'runtime_source_pins': pins},
        **accounting,
    }
    local_pins = {
        str(qualification / 'caller_r7.py'): '5' * 64,
        str(qualification / 'runtime_context.py'): '6' * 64,
        str(api_path): api_sha,
        str(caller_runtime_path): digest(caller_runtime_path.read_bytes()),
        str(context_api_path): digest(context_api_path.read_bytes()),
    }
    source_map = {
        'schema': 'provenance-v1', 'project_root': str(root),
        'canonical_state': str(state), 'code_root': str(tooling_root),
        'maintained_main': '4' * 40,
        'root_packets': selection, 'runtime_source_pins': pins,
        'accounting': accounting, 'local_source_pins': local_pins,
        'published_tooling': {'commit': tooling_commit,
                              'caller_api_path': str(api_path),
                              'caller_api_sha256': api_sha,
                              'caller_runtime_path': str(caller_runtime_path),
                              'caller_runtime_sha256': digest(caller_runtime_path.read_bytes()),
                              'context_api_path': str(context_api_path),
                              'context_api_sha256': digest(context_api_path.read_bytes())},
        'estimates_are_forecasts_only': True,
        'actual_job_authorized': True,
    }
    source_map['source_identity'] = digest(json.dumps(
        source_map, sort_keys=True, separators=(',', ':')).encode())
    source_path = work / 'source-map.json'
    source_raw = write_document(source_path, source_map)
    source_ref = {'path': str(source_path), 'sha256': digest(source_raw),
                  'source_identity': source_map['source_identity']}
    value = {
        'tooling_candidate_root': str(tooling_root),
        'caller_runtime_path': str(caller_runtime_path),
        'caller_runtime_sha256': digest(caller_runtime_path.read_bytes()),
        'candidate_context_api_sha256': digest(context_api_path.read_bytes()),
        'project_context_path': str(context_path), 'project_context_sha256': context_sha,
        'caller_api_path': str(api_path), 'caller_api_sha256': api_sha,
        'qualification_root': 'work/qualification', 'tooling_commit': tooling_commit,
        'caller_contract': {
            'successor_profile_schema': 'successor-profile-v1',
            'source_provenance_schema': 'provenance-v1',
            'historical_source_names': ['caller_r7.py', 'runtime_context.py'],
            'qualification_source_names': ['caller_r7.py'],
            'runtime_context_name': 'runtime_context.py',
            'caller_entry_name': 'caller_r7.py', 'profile_names': ['profile.json'],
            'promotion_name': 'promotion.json',
        },
    }
    profile = {
        'schema': 'successor-profile-v1', 'config': str(config_path),
        'project_root': str(root), 'canonical_state': str(state),
        'source_map': source_ref, 'future_job': str(job),
        'promoted_source_map': str(job / 'artifacts/promotion.json'),
        'artifacts': str(job / 'artifacts'), 'evidence': str(job / 'evidence'),
        'actual_job_authorized': True,
        'context_selection': bound['context_selection'],
    }
    profile_path = work / 'profile.json'
    profile_raw = write_document(profile_path, profile)
    config = {'argv': [str(executable), '-c', 'pass'],
              'sources': {str(executable): digest(executable.read_bytes()),
                          str(context_path): context_sha,
                          str(api_path): api_sha,
                          **local_pins,
                          **{ref['path']: ref['sha256'] for ref in selection.values()},
                          **pins,
                          **{item['path']: item['sha256'] for item in accounting.values()},
                          str(source_path): digest(source_raw),
                          str(profile_path): digest(profile_raw)}}

    class Base:
        @staticmethod
        def source_digest(path):
            return digest(Path(path).read_bytes())

        @staticmethod
        def pins(_pins):
            return None

    class Adaptive:
        base = Base()

    return (value, bound, config, config_path, job, profile_path, profile, source_path,
            source_map, Adaptive())


class GenericCallerBindingTests(unittest.TestCase):
    def test_qualification_root_accepts_canonical_nested_project_input_root(self):
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as temp:
            binding, binding_path, root, _state = make_binding(Path(temp))
            qualification = root / 'inputs' / 'caller'
            qualification.mkdir(parents=True)
            binding['qualification_root'] = 'inputs/caller'
            write_document(binding_path, binding)
            caller.configure(binding_path, expected_binding_sha256=digest(binding_path.read_bytes()))

            value, bound_root, _binding_sha256 = caller.binding()

            self.assertEqual(bound_root, root)
            self.assertEqual(value['qualification_root'], 'inputs/caller')

    def test_qualification_root_rejects_absolute_traversal_alias_and_symlink_escape(self):
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as temp:
            binding, binding_path, root, _state = make_binding(Path(temp))
            outside = Path(temp) / 'outside'
            outside.mkdir()
            (root / 'work' / 'escape').symlink_to(outside, target_is_directory=True)
            invalid = (str(root / 'work' / 'qualification'), '../outside',
                       'work/../outside', 'work//qualification',
                       'work/./qualification', '.', 'C:/outside',
                       'work\\qualification', 'work/escape/child')

            for relative in invalid:
                with self.subTest(qualification_root=relative):
                    changed = dict(binding, qualification_root=relative)
                    write_document(binding_path, changed)
                    caller.configure(binding_path,
                                     expected_binding_sha256=digest(binding_path.read_bytes()))
                    with self.assertRaisesRegex(caller.Refused, 'qualification_root'):
                        caller.binding()

    def test_root_packet_authorization_binding_requires_exact_boolean(self):
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as temp:
            binding, binding_path, _root, _state = make_binding(Path(temp))
            for authorized in (None, 1, 'true', []):
                with self.subTest(actual_job_authorized=authorized):
                    contract = dict(binding['root_packet_contract'],
                                    actual_job_authorized=authorized)
                    write_document(binding_path, dict(binding, root_packet_contract=contract))
                    caller.configure(binding_path,
                                     expected_binding_sha256=digest(binding_path.read_bytes()))
                    with self.assertRaisesRegex(caller.Refused, 'root_packet_contract'):
                        caller.binding()

    def test_successor_profile_accepts_root05_true_chain_and_checks_authorization_and_hashes(self):
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as temp:
            (value, bound, config, config_path, job, profile_path, profile, source_path,
             source_map, adaptive) = successor_profile_fixture(temp)
            accepted, source = caller.selected_profile(
                None, adaptive, bound, config_path, job, config, value,
                profile_path=profile_path, caller_argv=False)
            self.assertIs(accepted['actual_job_authorized'], True)
            self.assertEqual(source['source_identity'], source_map['source_identity'])

            original_raw = profile_path.read_bytes()
            mutations = {
                'false_authorization': ('actual_job_authorized', False),
                'integer_authorization': ('actual_job_authorized', 1),
                'string_authorization': ('actual_job_authorized', 'true'),
                'project_identity': ('project_root', '/other/project'),
                'state_identity': ('canonical_state', '/other/state'),
            }
            for name, (field, replacement) in mutations.items():
                with self.subTest(profile_mutation=name):
                    changed = dict(profile, **{field: replacement})
                    raw = write_document(profile_path, changed)
                    config['sources'][str(profile_path)] = digest(raw)
                    with self.assertRaisesRegex(caller.Refused, 'successor_profile_binding'):
                        caller.selected_profile(None, adaptive, bound, config_path, job,
                                                config, value, profile_path=profile_path,
                                                caller_argv=False)
            changed = json.loads(original_raw)
            changed['context_selection']['adoption']['sha256'] = 'f' * 64
            raw = write_document(profile_path, changed)
            config['sources'][str(profile_path)] = digest(raw)
            with self.assertRaisesRegex(caller.Refused, 'successor_profile_binding'):
                caller.selected_profile(None, adaptive, bound, config_path, job,
                                        config, value, profile_path=profile_path,
                                        caller_argv=False)
            profile_path.write_bytes(original_raw)
            config['sources'][str(profile_path)] = digest(original_raw)

            config['sources'][str(profile_path)] = 'f' * 64
            with self.assertRaisesRegex(caller.Refused, 'successor_profile_source_pin'):
                caller.selected_profile(None, adaptive, bound, config_path, job,
                                        config, value, profile_path=profile_path,
                                        caller_argv=False)
            config['sources'][str(profile_path)] = digest(original_raw)

            source_map['maintained_main'] = '9' * 40
            tampered = write_document(source_path, source_map)
            config['sources'][str(source_path)] = digest(tampered)
            with self.assertRaisesRegex(caller.Refused, 'successor_source_map_hash'):
                caller.selected_profile(None, adaptive, bound, config_path, job,
                                        config, value, profile_path=profile_path,
                                        caller_argv=False)

    def test_binding_refuses_external_symlink_project_markers(self):
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as temp:
            work = Path(temp)
            _binding, binding_path, root, _state = make_binding(work)
            caller.configure(binding_path, expected_binding_sha256=digest(binding_path.read_bytes()))
            for relative in ('AGENTS.md', '.factory/project-context.md'):
                with self.subTest(marker=relative):
                    marker = root / relative
                    external = work / f'external-{Path(relative).name}'
                    external.write_text('external marker\n', encoding='utf-8')
                    marker.unlink()
                    marker.symlink_to(external)
                    with self.assertRaisesRegex(caller.Refused, 'project_root'):
                        caller.binding()
                    marker.unlink()
                    marker.write_text('inert fixture\n', encoding='utf-8')

    def test_binding_changes_after_root_selection_refuse(self):
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as temp:
            _binding, binding_path, _root, _state = make_binding(Path(temp))
            trusted = digest(binding_path.read_bytes())
            caller.configure(binding_path, expected_binding_sha256=trusted)
            changed = json.loads(binding_path.read_text())
            changed['project_id'] = 'substituted-project'
            write_document(binding_path, changed)
            with self.assertRaisesRegex(caller.Refused, 'root_binding_trust'):
                caller.binding()

    def test_inert_second_project_reaches_legacy_public_run_status_stop(self):
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as temp:
            work = Path(temp)
            binding, binding_path, root, _state = make_binding(work)
            caller.configure(binding_path, expected_binding_sha256=digest(binding_path.read_bytes()))
            caller.binding()
            config_path = root / 'work/run-config.json'
            job_path = root / 'work/jobs/future'
            config = {'cwd': str(root / 'work'), 'sources': {str(binding_path): digest(binding_path.read_bytes())}}
            write_document(config_path, config)
            runtime = FakeRuntime(config, job_path)
            with patch.object(caller, 'load_runtime', return_value=runtime), \
                 patch.object(subprocess, 'Popen', side_effect=AssertionError('native launch forbidden')):
                launched = caller.dispatch('run', str(job_path), str(config_path))
                self.assertFalse(job_path.exists())
                job_path.mkdir()
                status = caller.dispatch('status', str(job_path))
                stopped = caller.dispatch('stop', str(job_path))
            self.assertEqual(launched['state'], 'MOCK-LAUNCH')
            self.assertEqual(status['state'], 'MOCK-STATUS')
            self.assertEqual(stopped['state'], 'MOCK-STOP')
            self.assertEqual([call[0] for call in runtime.calls], [
                'configuration', 'launch', 'load_job', 'public_status', 'load_job', 'stop'])
            self.assertEqual(binding['project_id'], 'inert-example-project')
            self.assertTrue(job_path.is_dir())

    def test_false_packet_context_binds_before_refused_run_and_inert_status_stop(self):
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as temp:
            work = Path(temp)
            binding, binding_path, root, state = make_binding(work, packets=True)
            caller.configure(binding_path, expected_binding_sha256=digest(binding_path.read_bytes()))
            caller.binding()

            executable = Path(sys.executable).resolve(strict=True)
            config_path = root / 'work/run-config.json'
            job_path = root / 'work/jobs/run-refused'
            input_path = root / 'work/input.txt'
            write_private(input_path, b'inert input\n')
            config = {
                'version': 2, 'argv': [str(executable), '-c', 'pass'],
                'cwd': str(root / 'work'),
                'limits': {'memory_bytes': 'infinity', 'cpu_percent': 'infinity',
                           'address_space_bytes': 'infinity', 'runtime_seconds': 'infinity'},
                'sources': {str(executable): digest(executable.read_bytes()),
                            str(input_path): digest(input_path.read_bytes())},
                'inputs': [str(input_path)],
                'estimates': {'memory_bytes': 0, 'disk_bytes': 0, 'inodes': 0},
                'foreground': True, 'cooperative': {'enabled': True},
                'owner': 'inert-fixture', 'lease': 'inert-fixture', 'kind': 'analysis',
            }
            write_document(config_path, config)
            status_job = root / 'work/jobs/status-fixture'
            status_job.mkdir(mode=0o700)
            expected_intent = {
                'contract_version': 2, 'candidate': str(ADAPTIVE_ROOT),
                'state_dir': str(state), 'project_root': str(root),
                'job_id': 'status-fixture', 'unit': 'inert-task-status-fixture.service',
                'config': {'version': 2, 'argv': [str(executable), '-c', 'pass'],
                           'cwd': str(root / 'work'), 'sources': {}, 'inputs': [],
                           'limits': config['limits']},
            }
            original_load_runtime = caller.load_runtime

            def load_real_generic_with_fixture_intent(value):
                runtime = original_load_runtime(value)
                runtime.load_job = lambda _job: (status_job, expected_intent)
                return runtime

            context_bindings, advances, stops = [], [], []
            original_load_root = caller.load_root_runtime

            def bound_runtime_with_inert_terminal_methods(*args, **kwargs):
                context, bound, adaptive = original_load_root(*args, **kwargs)
                context_bindings.append((context, bound, adaptive))
                adaptive.launch = lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError('adaptive launch forbidden'))
                adaptive.advance = lambda job: advances.append(Path(job)) or {'state': 'INERT-STATUS'}
                adaptive.stop = lambda job: stops.append(Path(job)) or {'state': 'INERT-STOP'}
                return context, bound, adaptive

            with patch.object(caller, 'load_root_runtime',
                              side_effect=bound_runtime_with_inert_terminal_methods), \
                 patch.object(caller, 'load_runtime', side_effect=load_real_generic_with_fixture_intent), \
                 patch.object(subprocess, 'Popen', side_effect=AssertionError('native launch forbidden')):
                with self.assertRaisesRegex(caller.Refused, 'actual_job_unauthorized'):
                    caller.dispatch('run', str(job_path), str(config_path))
                self.assertFalse(job_path.exists())
                self.assertEqual(caller.dispatch('status', str(status_job)),
                                 {'state': 'INERT-STATUS'})
                self.assertEqual(caller.dispatch('stop', str(status_job)),
                                 {'state': 'INERT-STOP'})

            self.assertEqual(len(context_bindings), 3)
            for _context, bound, adaptive in context_bindings:
                self.assertEqual(bound['project_root'], root)
                self.assertEqual(bound['canonical_state'], state)
                self.assertEqual(bound['project_id'], 'inert-example-project')
                self.assertIs(bound['adoption']['actual_job_authorized'], False)
                self.assertEqual(adaptive.PROJECT_ID, 'inert-example-project')
                self.assertEqual(bound['historical_runtime'].LEGACY_PROJECT_BINDING['unit_prefix'],
                                 'inert-task')
            self.assertEqual(advances, [status_job])
            self.assertEqual(stops, [status_job])
            self.assertFalse(job_path.exists())


if __name__ == '__main__':
    unittest.main()
