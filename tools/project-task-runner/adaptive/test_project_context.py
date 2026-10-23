"""Offline project binding checks; no native units or live state are used."""
from contextlib import contextmanager
from contextlib import redirect_stdout
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
CONTEXT_SPEC = importlib.util.spec_from_file_location(
    'generic_project_context_helpers', HERE.parent / 'project_context.py')
context_helpers = importlib.util.module_from_spec(CONTEXT_SPEC)
CONTEXT_SPEC.loader.exec_module(context_helpers)
CALLER_API_SPEC = importlib.util.spec_from_file_location(
    'generic_project_caller_api', HERE.parent / 'caller_api.py')
caller_api = importlib.util.module_from_spec(CALLER_API_SPEC)
CALLER_API_SPEC.loader.exec_module(caller_api)


def load():
    spec = importlib.util.spec_from_file_location('project_context_runner', HERE / 'runner.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@contextmanager
def cwd(path):
    previous = Path.cwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(previous)


class ProjectContext(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(self.temp.cleanup)
        self.run = load()
        self.roots = [Path(self.temp.name) / name for name in ('first', 'second')]
        for root in self.roots:
            root.mkdir(mode=0o700)
            state = root / 'state'
            state.mkdir(mode=0o700)
            self.run.base.publish(state / 'capacity.json', {'version': 3, 'jobs': {}, 'policy': None})

    def config(self, root):
        executable = Path('/usr/bin/true').resolve()
        config = {'version': 2, 'argv': [str(executable)], 'cwd': str(root),
                  'limits': {'memory_bytes': 'infinity', 'cpu_percent': 'infinity',
                             'address_space_bytes': 'infinity', 'runtime_seconds': 'infinity'},
                  'sources': {str(executable): self.run.base.source_digest(executable)},
                  'owner': 'offline-fixture', 'lease': 'private', 'kind': 'command',
                  'inputs': [str(executable)], 'estimates': {'memory_bytes': 1, 'disk_bytes': 0, 'inodes': 0},
                  'foreground': False, 'cooperative': {'enabled': True}}
        path = root / 'config.json'
        self.run.base.publish(path, config)
        return path

    def packet_chain(self, root, authorized):
        state = 'ROOT05_SUCCESSOR' if authorized else 'ROOT04_LEGACY'
        refs = {}
        for name in ('source_evidence', 'candidate', 'adoption'):
            refs[name] = {'path': str(root / 'work' / f'{name}.json'),
                          'sha256': name[0] * 64}
        pins = {'/tooling/runtime.py': 'a' * 64}
        evidence = {'source': {'observed_merge': 'b' * 40},
                    'runtime_source_pins': pins}
        candidate = {'project_root': str(root), 'canonical_state': str(root / 'state'),
                     'source_evidence': refs['source_evidence'],
                     'runtime_source_pins': pins}
        adoption = {
            'project_root': str(root), 'canonical_state': str(root / 'state'),
            'code_root': '/tooling', 'runtime_path': '/tooling/adaptive',
            'state': state, 'actual_job_authorized': authorized,
            'actual_runtime_adoption_authorized': True,
            'candidate': refs['candidate']['path'],
            'candidate_sha256': refs['candidate']['sha256'],
            'source_validation': {'root_source_evidence': refs['source_evidence']},
            'runtime_source_pins': pins,
        }
        packets = {'source_evidence': evidence, 'candidate': candidate, 'adoption': adoption}
        contract = {
            'packets': {name: {'path': str(Path(ref['path']).relative_to(root)),
                               'sha256': ref['sha256']} for name, ref in refs.items()},
            'actual_job_authorized': authorized,
            'observed_merge': evidence['source']['observed_merge'],
            'canonical_state': str(root / 'state'), 'code_root': '/tooling',
            'runtime_path': '/tooling/adaptive', 'adoption_state': state,
        }
        return packets, refs, contract

    def test_root_packet_chain_accepts_exact_true_and_preserves_legacy_false(self):
        root = self.roots[0]
        for authorized in (True, False):
            with self.subTest(actual_job_authorized=authorized):
                packets, refs, contract = self.packet_chain(root, authorized)
                bound = context_helpers.validate_root_packet_chain(
                    packets, refs, contract, root)
                self.assertIs(bound['adoption']['actual_job_authorized'], authorized)

    def test_root_packet_chain_refuses_missing_or_mismatched_pins_and_non_boolean_auth(self):
        root = self.roots[0]
        for malformed in ('missing_ref', 'wrong_ref_hash', 'contract_non_boolean',
                          'adoption_non_boolean', 'runtime_pin_mismatch',
                          'project_identity_mismatch'):
            with self.subTest(malformed=malformed):
                packets, refs, contract = self.packet_chain(root, True)
                if malformed == 'missing_ref':
                    del refs['candidate']
                elif malformed == 'wrong_ref_hash':
                    refs['candidate'] = dict(refs['candidate'], sha256='f' * 64)
                elif malformed == 'contract_non_boolean':
                    contract['actual_job_authorized'] = 1
                elif malformed == 'adoption_non_boolean':
                    packets['adoption']['actual_job_authorized'] = 1
                elif malformed == 'runtime_pin_mismatch':
                    packets['candidate']['runtime_source_pins'] = {'/tooling/runtime.py': 'f' * 64}
                else:
                    packets['candidate']['project_root'] = str(root / 'other')
                with self.assertRaises(ValueError):
                    context_helpers.validate_root_packet_chain(
                        packets, refs, contract, root)

    def test_binding_clears_stale_cross_project_legacy_binding(self):
        class Runtime:
            PROJECT_ROOT = None
            CANONICAL_STATE = None
            CODE_ROOT = None
            PROJECT_ID = 'old-project'
            LEGACY_PROJECT_BINDING = {'project_id': 'old-project'}

            @staticmethod
            def bind_context(root, state):
                Runtime.PROJECT_ROOT = root
                Runtime.CANONICAL_STATE = state
                Runtime.CODE_ROOT = HERE.parents[2]
                return state

        root = self.roots[0]
        context_helpers.bind_project_context(
            Runtime, root, root / 'state', 'first-project', HERE.parents[2])
        self.assertIsNone(Runtime.LEGACY_PROJECT_BINDING)
        self.assertEqual(Runtime.PROJECT_ID, 'first-project')

    def enqueue(self, root, explicit=True, project_binding=None):
        with patch.object(self.run.base, 'manager_environment', return_value=({}, 'fixture')), \
             patch.object(self.run, 'common_cgroup', return_value=Path('/unused')), \
             patch.object(self.run.policy, 'sample_host', return_value={'missing': ['offline.fixture']}) as sample, \
             patch.object(self.run, 'start_unit', side_effect=AssertionError('offline fixture must not launch')):
            config = self.config(root)
            result = (self.run.launch(root / 'job', config, root / 'state', root,
                                      project_binding=project_binding) if explicit
                      else self.run.launch(root / 'job', config, root / 'state',
                                           project_binding=project_binding))
            self.assertEqual(result['state'], 'READY')
            self.assertEqual(result['capacity_status'], 'unknown')
            self.assertEqual(sample.call_args.args[0], root)
        return result

    def reap_helper(self, helper):
        if helper is not None:
            if 'gate' in helper:
                os.close(helper.pop('gate'))
            try:
                os.waitpid(helper['process']['pid'], 0)
            except ChildProcessError:
                pass
            finally:
                if 'pidfd' in helper:
                    os.close(helper['pidfd'])

    def helper_exited(self, helper):
        self.assertTrue(select.select([helper['pidfd']], [], [], 2)[0], 'helper did not exit')
        counters = {}
        result = self.run.poll_helper(helper, counters)
        if result is None:
            helper.pop('pidfd')
        self.assertIsNone(result)
        self.assertEqual(counters, {'helpers_reaped': 1, 'helper_last_status': 0})
        with self.assertRaises(ChildProcessError):
            os.waitpid(helper['process']['pid'], os.WNOHANG)

    def test_real_helper_waits_for_parent_gate_before_reconciliation(self):
        root = self.roots[0]
        state = self.run.bind_context(root, root / 'state')
        marker = root / 'reconciled'
        reader, writer = os.pipe()
        parent = os.getpid()
        read = os.read
        def entering_read(fd, count):
            if os.getpid() != parent:
                os.write(writer, b'R')
            return read(fd, count)
        helper = None
        try:
            with patch.object(self.run.os, 'read', side_effect=entering_read), \
                 patch.object(self.run, 'reconcile', side_effect=lambda *args: marker.write_text('released')):
                helper = self.run.start_helper({'state_dir': str(state)}, {})
                os.close(writer)
                writer = None
                self.assertTrue(select.select([reader], [], [], 2)[0], 'child did not reach the gate')
                self.assertEqual(read(reader, 1), b'R')
                self.assertFalse(marker.exists())
                self.assertFalse((state / 'capacity.lock').exists())
                self.assertEqual(os.waitpid(helper['process']['pid'], os.WNOHANG), (0, 0))
                self.run.release_helper(helper)
                self.helper_exited(helper)
                helper = None
                self.assertEqual(marker.read_text(), 'released')
        finally:
            self.reap_helper(helper)
            os.close(reader)
            if writer is not None:
                os.close(writer)

    def test_real_helper_identity_refusal_closes_gate_and_reaps_without_reconciliation(self):
        root = self.roots[0]
        state = self.run.bind_context(root, root / 'state')
        marker = root / 'unexpected-reconciliation'
        before = (state / 'capacity.json').read_bytes()
        process = self.run.base.process
        parent = os.getpid()
        children = []
        def wrong_identity(pid):
            identity = process(pid)
            if pid != parent:
                children.append(pid)
                identity['uid'] = -1
            return identity
        with patch.object(self.run.base, 'process', side_effect=wrong_identity), \
             patch.object(self.run, 'reconcile', side_effect=lambda *args: marker.write_text('unexpected')):
            with self.assertRaisesRegex(self.run.base.Refused, 'helper_handoff_refused_before_reconcile_child_reaped'):
                self.run.start_helper({'state_dir': str(state)}, {})
        self.assertEqual(len(children), 1)
        with self.assertRaises(ChildProcessError):
            os.waitpid(children[0], os.WNOHANG)
        self.assertFalse(Path('/proc', str(children[0])).exists())
        self.assertFalse(marker.exists())
        self.assertFalse((state / 'capacity.lock').exists())
        self.assertEqual((state / 'capacity.json').read_bytes(), before)

    def test_real_helper_busy_project_flock_exits_without_reconciliation_or_ledger_change(self):
        root = self.roots[0]
        state = self.run.bind_context(root, root / 'state')
        marker = root / 'unexpected-reconciliation'
        before = (state / 'capacity.json').read_bytes()
        helper = None
        try:
            with self.run.locked(state), \
                 patch.object(self.run, 'reconcile', side_effect=lambda *args: marker.write_text('unexpected')):
                helper = self.run.start_helper({'state_dir': str(state)}, {})
                self.run.release_helper(helper)
                self.helper_exited(helper)
                helper = None
                self.assertFalse(marker.exists())
                self.assertEqual((state / 'capacity.json').read_bytes(), before)
        finally:
            self.reap_helper(helper)

    def test_import_selects_no_project_or_state(self):
        self.assertIsNone(self.run.ROOT)
        self.assertIsNone(self.run.PROJECT_ROOT)
        self.assertIsNone(self.run.CANONICAL_STATE)
        self.assertEqual(self.run.CODE_ROOT, HERE.parents[2])
        self.assertTrue(self.run.source_pins())

    def test_inert_second_project_binding_uses_only_explicit_project_and_state(self):
        second = self.roots[1]
        state = self.run.bind_context(second, second / 'state')

        self.assertEqual(self.run.PROJECT_ROOT, second.resolve())
        self.assertEqual(self.run.ROOT, second.resolve())
        self.assertEqual(self.run.CANONICAL_STATE, (second / 'state').resolve())
        self.assertEqual(state, self.run.CANONICAL_STATE)
        self.assertEqual(self.run.PROJECT_ROOT.name, 'second')

    def test_generic_context_helper_passes_project_identity_to_runtime(self):
        second = self.roots[1]
        state = context_helpers.bind_project_context(
            self.run, second, second / 'state', 'second-project', HERE.parents[2])
        self.assertEqual(state, (second / 'state').resolve())
        self.assertEqual(self.run.PROJECT_ID, 'second-project')

    def test_packet_set_reads_and_hashes_one_bounded_pinned_snapshot(self):
        root = self.roots[0]
        packet = root / 'work' / 'packets' / 'source.json'
        packet.parent.mkdir(parents=True)
        raw = b'{"generation":1}\n'
        packet.write_bytes(raw)

        class API:
            def __init__(self): self.reads = []
            def read_pinned_source(self, path, expected, *, limit):
                self.reads.append((Path(path), expected, limit))
                captured = Path(path).read_bytes()
                if __import__('hashlib').sha256(captured).hexdigest() != expected:
                    raise ValueError('pin drift')
                return captured

        api = API()
        import hashlib
        descriptors = {'source_evidence': {
            'path': 'work/packets/source.json',
            'sha256': hashlib.sha256(raw).hexdigest()}}
        packets, references = context_helpers.read_pinned_packet_set(
            descriptors, root, api, limit=128)
        self.assertEqual(packets['source_evidence'], {'generation': 1})
        self.assertEqual(references['source_evidence']['sha256'], descriptors['source_evidence']['sha256'])
        self.assertEqual(len(api.reads), 1)
        self.assertEqual(api.reads[0][2], 128)

    def test_packet_set_refuses_tampered_bytes_before_packet_validation(self):
        root = self.roots[0]
        packet = root / 'work' / 'packets' / 'adoption.json'
        packet.parent.mkdir(parents=True)
        original = b'{"actual_job_authorized":true}\n'
        packet.write_bytes(original)
        descriptor = {'adoption': {
            'path': 'work/packets/adoption.json',
            'sha256': __import__('hashlib').sha256(original).hexdigest()}}
        packet.write_bytes(b'{"actual_job_authorized":false}\n')

        with self.assertRaisesRegex(caller_api.Refused, 'source_pin_before_execute'):
            context_helpers.read_pinned_packet_set(descriptor, root, caller_api)

    def test_second_project_binding_drives_public_run_status_stop_and_payload_paths(self):
        second = self.roots[1]
        binding = {'project_id': 'second-project', 'unit_prefix': 'second-task',
                   'payload_environment': {'SECOND_TASK_SCRATCH': '{job}/scratch'}}
        state = context_helpers.bind_project_context(
            self.run, second, second / 'state', 'second-project', HERE.parents[2],
            legacy_project_binding=binding)
        config = self.config(second)

        def cli(args):
            output = io.StringIO()
            with patch.object(self.run.sys, 'argv', ['runner.py', *args]), \
                 patch.object(self.run.base, 'manager_environment', return_value=({}, 'fixture')), \
                 patch.object(self.run, 'common_cgroup', return_value=Path('/unused')), \
                 patch.object(self.run.policy, 'sample_host', return_value={'missing': ['offline.fixture']}), \
                 patch.object(self.run, 'start_unit', side_effect=AssertionError('offline fixture must not launch')), \
                 redirect_stdout(output):
                code = self.run.main()
            self.assertEqual(code, 0, output.getvalue())
            return json.loads(output.getvalue())

        result = cli(['run', '--job', str(second / 'job'), '--config', str(config),
                      '--state', str(state), '--project-root', str(second)])
        intent = self.run.base.read(second / 'job/intent.json')
        self.assertEqual(intent['project_binding'], binding)
        self.assertEqual(intent['unit'], 'second-task-' + result['job_id'] + '.service')
        self.assertEqual(self.run.payload_environment(intent, second / 'job'),
                         {'SECOND_TASK_SCRATCH': str(second / 'job/scratch')})
        status = cli(['status', '--job', str(second / 'job')])
        stopped = cli(['stop', '--job', str(second / 'job')])
        self.assertEqual(status['job_id'], result['job_id'])
        self.assertEqual(stopped['state'], 'CANCELLED')

    def test_generic_context_and_runtime_have_no_wow_identity_or_path_assumptions(self):
        for path in (HERE.parent / 'project_context.py', HERE / 'runner.py',
                     HERE.parent / 'runner.py', HERE.parent / 'caller_api.py'):
            source = path.read_bytes().lower()
            for forbidden in (b'wow-private-server', b'wow_task_',
                              b'/wow private server/', b'root-resume-20261020'):
                self.assertNotIn(forbidden, source, f'{path}: {forbidden!r}')

    def test_legacy_v2_intent_uses_explicitly_bound_payload_environment(self):
        root = self.roots[0]
        legacy = {'project_id': 'wow-private-server', 'unit_prefix': 'wow-task',
                  'payload_environment': {
                      'WOW_TASK_CONTROL_FILE': '{job}/control.json',
                      'WOW_TASK_SCRATCH_DIR': '{job}/scratch'}}
        self.run.bind_context(root, root / 'state', 'wow-private-server', legacy)
        job = root / 'legacy-job'
        job.mkdir(mode=0o700)
        intent = {'job_id': 'legacy-1', 'unit': 'wow-task-legacy-1.service'}
        self.assertEqual(self.run.payload_environment(intent, job), {
            'WOW_TASK_CONTROL_FILE': str(job / 'control.json'),
            'WOW_TASK_SCRATCH_DIR': str(job / 'scratch')})

    def test_job_paths_stay_inside_project_before_config_or_ledger_work(self):
        root = self.roots[0]
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir(mode=0o700)
        (root / 'escape').symlink_to(outside, target_is_directory=True)
        before = (root / 'state/capacity.json').read_bytes()
        for job in (outside / 'job', root / 'escape/job'):
            with self.subTest(job=job), \
                 patch.object(self.run, 'locked') as lock, \
                 patch.object(self.run, 'configuration') as config:
                with self.assertRaisesRegex(self.run.base.Refused, 'job_outside_project'):
                    self.run.launch(job, 'unused', root / 'state', root)
                lock.assert_not_called()
                config.assert_not_called()
                self.assertFalse((outside / 'job').exists())
                self.assertEqual((root / 'state/capacity.json').read_bytes(), before)

    def test_resume_paths_refuse_job_moved_outside_project_before_host_work(self):
        root = self.roots[0]
        self.enqueue(root)
        external = Path(self.temp.name) / 'external-job'
        shutil.copytree(root / 'job', external)
        intent_path = external / 'intent.json'
        intent = self.run.base.read(intent_path)
        intent['job_dir'] = str(external)
        self.run.replace(intent_path, intent)
        for operation in (self.run.advance, self.run.stop, self.run.worker):
            with self.subTest(operation=operation.__name__), \
                 patch.object(self.run.base, 'pins') as pins, \
                 patch.object(self.run.base, 'manager_environment') as manager, \
                 patch.object(self.run, 'locked') as lock:
                with self.assertRaisesRegex(self.run.base.Refused, 'job_outside_project'):
                    operation(external)
                pins.assert_not_called()
                manager.assert_not_called()
                lock.assert_not_called()

    def test_unrecorded_jobs_refuse_before_reconciliation_or_unit_stop(self):
        root = self.roots[0]
        result = self.enqueue(root)
        ledger_path = root / 'state/capacity.json'
        ledger = self.run.base.read(ledger_path)
        del ledger['jobs'][result['job_id']]
        self.run.replace(ledger_path, ledger)

        with patch.object(self.run.base, 'manager_environment', return_value=({}, 'fixture')), \
             patch.object(self.run.policy, 'sample_host') as sample, \
             patch.object(self.run, 'replace') as write:
            with self.assertRaisesRegex(self.run.base.Refused, 'job_ledger_mismatch'):
                self.run.advance(root / 'job')
            sample.assert_not_called()
            write.assert_not_called()

        self.run.base.publish(root / 'job/ready.json', {})
        before = ledger_path.read_bytes()
        with patch.object(self.run.base, 'manager_environment') as manager, \
             patch.object(self.run.base, 'unit_show') as unit_show, \
             patch.object(self.run.base, 'stop') as stop:
            with self.assertRaisesRegex(self.run.base.Refused, 'job_ledger_mismatch'):
                self.run.stop(root / 'job')
            manager.assert_not_called()
            unit_show.assert_not_called()
            stop.assert_not_called()
        self.assertEqual(ledger_path.read_bytes(), before)

    def test_ready_stop_validates_ledger_then_cleans_without_holding_lock(self):
        root = self.roots[0]
        result = self.enqueue(root)
        job = root / 'job'
        self.run.base.publish(job / 'ready.json', {})
        def cancel(owned_job):
            self.assertEqual(owned_job, job)
            with self.run.locked(root / 'state', nonblocking=True):
                pass
            return {'state': 'CANCELLED'}
        with patch.object(self.run.base, 'manager_environment', return_value=({}, 'fixture')), \
             patch.object(self.run.base, 'unit_show', return_value={'MainPID': '0'}), \
             patch.object(self.run.base, 'custody', return_value={}), \
             patch.object(self.run.base, 'stop', side_effect=cancel) as stop, \
             patch.object(self.run, 'terminal_proof'), \
             patch.object(self.run, 'cleanup_terminal', return_value={}):
            self.assertEqual(self.run.stop(job)['state'], 'CANCELLED')
            stop.assert_called_once_with(job)
        ledger = self.run.base.read(root / 'state/capacity.json')
        self.assertEqual(ledger['jobs'][result['job_id']]['state'], 'terminal')

    def test_missing_and_legacy_ledgers_refuse_without_mutation(self):
        root = self.roots[0]
        missing = root / 'missing-state'
        missing.mkdir(mode=0o700)
        self.run.bind_context(root, missing)
        with self.assertRaisesRegex(self.run.base.Refused, 'reviewed_ledger_migration_required'):
            self.run.read_ledger(missing)
        self.assertEqual(list(missing.iterdir()), [])

        legacy = root / 'legacy-state'
        legacy.mkdir(mode=0o700)
        path = legacy / 'capacity.json'
        self.run.base.publish(path, {'version': 2, 'jobs': {}})
        before = path.read_bytes()
        self.run.bind_context(root, legacy)
        with self.assertRaisesRegex(self.run.base.Refused, 'ledger_version'):
            self.run.read_ledger(legacy)
        self.assertEqual(path.read_bytes(), before)

    def test_resume_refuses_changed_source_pin_before_state_or_host_work(self):
        root = self.roots[0]
        result = self.enqueue(root)
        pins = self.run.source_pins()
        expected = {
            str(HERE / 'runner.py'), str(HERE / 'policy.py'), str(HERE / 'retention.py'),
            str(HERE.parents[2] / 'tools/project-task-runner/runner.py'),
            str(HERE.parents[2] / 'scripts/project-task-runner'), str(Path(sys.executable).resolve()),
        }
        self.assertTrue(expected <= pins.keys())

        source = Path(self.temp.name) / 'pinned-source.py'
        source.write_text('before')
        intent_path = root / 'job/intent.json'
        intent = self.run.base.read(intent_path)
        intent['source_pins'][str(source)] = self.run.base.source_digest(source)
        self.run.replace(intent_path, intent)
        source.write_text('after')
        with patch.object(self.run.base, 'manager_environment') as manager, \
             patch.object(self.run, 'locked') as lock:
            with self.assertRaisesRegex(self.run.base.Refused, 'source_changed'):
                self.run.advance(root / 'job')
            manager.assert_not_called()
            lock.assert_not_called()

    def test_two_contexts_launch_status_stop_touch_only_own_existing_store(self):
        identities = []
        for root in self.roots:
            other = next(p for p in self.roots if p != root)
            other_before = (other / 'state/capacity.json').read_bytes()
            result = self.enqueue(root)
            identities.append(result['job_id'])
            intent = self.run.base.read(root / 'job/intent.json')
            self.assertEqual(intent['project_root'], str(root))
            self.assertEqual(intent['state_dir'], str(root / 'state'))
            with cwd(other), patch.object(self.run.base, 'manager_environment', return_value=({}, 'fixture')), \
                 patch.object(self.run, 'common_cgroup', return_value=Path('/unused')), \
                 patch.object(self.run.policy, 'sample_host', return_value={'missing': ['offline.fixture']}) as sample, \
                 patch.object(self.run, 'start_unit', side_effect=AssertionError('offline fixture must not launch')):
                self.assertEqual(self.run.advance(root / 'job')['job_id'], result['job_id'])
                self.assertEqual(sample.call_args.args[0], root)
                self.assertEqual(self.run.stop(root / 'job')['state'], 'CANCELLED')
            self.assertEqual((other / 'state/capacity.json').read_bytes(), other_before)
            ledger = self.run.base.read(root / 'state/capacity.json')
            self.assertEqual(set(ledger['jobs']), {result['job_id']})
            self.assertEqual(ledger['jobs'][result['job_id']]['state'], 'cancelled-before-launch')
        self.assertNotEqual(*identities)

    def test_cwd_default_preserves_existing_in_project_run_form(self):
        root = self.roots[0]
        with cwd(root):
            self.enqueue(root, explicit=False)
        self.assertEqual(self.run.ROOT, root)

    def test_project_relative_configuration_uses_bound_root(self):
        first, second = self.roots
        path = first / 'src/narrow'
        path.mkdir(parents=True)
        config_path = self.config(first)
        config = self.run.base.read(config_path)
        config.update(kind='read', inputs=[str(path)])
        self.run.replace(config_path, config)
        self.run.bind_context(first, first / 'state')
        self.assertEqual(self.run.configuration(config_path)[0]['inputs'], [str(path)])
        self.run.bind_context(second, second / 'state')
        with self.assertRaises(ValueError):
            self.run.configuration(config_path)

    def test_cross_root_refuses_before_lock_config_or_ledger(self):
        first, second = self.roots
        before = [(root / 'state/capacity.json').read_bytes() for root in self.roots]
        with patch.object(self.run, 'locked') as lock, patch.object(self.run, 'configuration') as config:
            with self.assertRaisesRegex(self.run.base.Refused, 'state_outside_project'):
                self.run.launch(first / 'job', 'unused', second / 'state', first)
            lock.assert_not_called()
            config.assert_not_called()
        self.assertEqual(before, [(root / 'state/capacity.json').read_bytes() for root in self.roots])
        self.assertFalse((first / 'job').exists())
        self.assertFalse(any((root / 'state/capacity.lock').exists() for root in self.roots))

    def test_missing_state_and_missing_root_are_not_created(self):
        root = self.roots[0]
        for project, state in ((root, root / 'missing'), (root / 'missing', root / 'state')):
            with self.subTest(project=project, state=state), self.assertRaises(FileNotFoundError):
                self.run.bind_context(project, state)
            self.assertFalse((root / 'missing').exists())

    def test_public_or_symlink_state_refuses(self):
        root = self.roots[0]
        public = root / 'public'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        alias = root / 'alias'
        alias.symlink_to(root / 'state', target_is_directory=True)
        for state in (public, alias):
            with self.subTest(state=state), self.assertRaisesRegex(self.run.base.Refused, 'private_directory'):
                self.run.bind_context(root, state)

    def test_root_resolves_strictly_and_state_cannot_use_prefix_sibling(self):
        root = self.roots[0]
        self.assertEqual(self.run.bind_context(root / '..' / root.name, root / 'state'), root / 'state')
        sibling = root.with_name(root.name + '-sibling')
        sibling.mkdir(mode=0o700)
        with self.assertRaisesRegex(self.run.base.Refused, 'state_outside_project'):
            self.run.bind_context(root, sibling)

    def test_worker_restores_intent_binding_before_environment_or_host_work(self):
        root = self.roots[0]
        self.enqueue(root)
        self.run.bind_context(self.roots[1], self.roots[1] / 'state')
        read = self.run.base.read
        def read_before_host(path):
            if path.name == 'environment.json':
                self.assertEqual(self.run.ROOT, root)
                self.assertEqual(self.run.CANONICAL_STATE, root / 'state')
                raise RuntimeError('fixture stops before host work')
            return read(path)
        with patch.object(self.run.base, 'read', side_effect=read_before_host), \
             self.assertRaisesRegex(RuntimeError, 'fixture stops before host work'):
            self.run.worker(root / 'job')

    def test_worker_passes_custom_project_binding_to_payload_subprocess(self):
        from types import SimpleNamespace

        root = self.roots[0]
        job = root / 'bound-job'
        job.mkdir(mode=0o700)
        (job / 'environment.json').write_text('{}', encoding='utf-8')
        (job / 'environment.json').chmod(0o600)
        state = root / 'state'
        project_binding = {'project_id': 'second-project', 'unit_prefix': 'second-task',
                           'payload_environment': {
                               'SECOND_TASK_CONTROL': '{job}/control.json',
                               'SECOND_TASK_SCRATCH': '{job}/scratch'}}
        intent = {'contract_version': 2, 'candidate': str(self.run.HERE),
                  'project_root': str(root), 'state_dir': str(state), 'job_id': 'run-1',
                  'unit': 'second-task-run-1.service', 'project_binding': project_binding,
                  'source_pins': {}, 'topology': 'none', 'config': {'argv': ['/usr/bin/true'], 'cwd': str(root),
                    'sources': {}, 'estimates': {'memory_bytes': 1}, 'owner': 'fixture'}}
        self.run.base.publish(job / 'intent.json', intent)
        cg = '/user.slice/user-1000.slice/user@1000.service/second-task-run-1.service'
        identity = {'pid': os.getpid(), 'uid': os.getuid(), 'start_ticks': 1, 'cgroup': cg}
        captured = {}

        class StopBeforePayload(Exception):
            pass

        def admit(job_path, _cancelled, _deadline):
            ready = self.run.base.read(job_path / 'ready.json')
            self.run.base.publish(job_path / 'admission.json', {
                'intent_sha256': ready['intent_sha256'],
                'invocation_id': ready['invocation_id']})

        def publish(path, value):
            Path(path).write_text(json.dumps(value), encoding='utf-8')
            Path(path).chmod(0o600)

        def capture_popen(*args, **kwargs):
            captured['args'] = args
            captured['kwargs'] = kwargs
            raise StopBeforePayload()

        with patch.object(self.run.base, 'load_job', return_value=(job, intent)), \
             patch.object(self.run.base, 'process', return_value=identity), \
             patch.object(self.run.base, 'unit_show', return_value={
                 'MainPID': str(os.getpid()), 'ControlGroup': cg,
                 'InvocationID': 'fixture-invocation', 'Description': 'second-task run-1'}), \
             patch.object(self.run.base, 'management_cgroup', return_value=cg), \
             patch.object(self.run.base, 'pins'), \
             patch.object(self.run.base, 'admission_wait_ok', side_effect=admit), \
             patch.object(self.run.base, 'cleanup_members', return_value={'quiescent': True, 'errors': []}), \
             patch.object(self.run.base, 'publish', side_effect=publish), \
             patch.object(self.run.ctypes, 'CDLL', return_value=SimpleNamespace(prctl=lambda *args: 0)), \
             patch.object(self.run.subprocess, 'Popen', side_effect=capture_popen), \
             patch.dict(os.environ, {'INVOCATION_ID': 'fixture-invocation'}):
            self.run.worker(job)

        payload_env = captured['kwargs']['env']
        self.assertEqual(payload_env['SECOND_TASK_CONTROL'], str(job / 'control.json'))
        self.assertEqual(payload_env['SECOND_TASK_SCRATCH'], str(job / 'scratch'))

    def test_mismatched_intent_refuses_all_resume_paths_before_lock(self):
        first, second = self.roots
        self.enqueue(first)
        intent_path = first / 'job/intent.json'
        intent = self.run.base.read(intent_path)
        intent['project_root'] = str(second)
        self.run.replace(intent_path, intent)
        for operation in (self.run.advance, self.run.stop, self.run.worker):
            with self.subTest(operation=operation.__name__), patch.object(self.run, 'locked') as lock:
                with self.assertRaisesRegex(self.run.base.Refused, 'state_outside_project'):
                    operation(first / 'job')
                lock.assert_not_called()

    def test_historical_intent_without_root_uses_cwd_and_keeps_custody(self):
        root = self.roots[0]
        self.enqueue(root)
        path = root / 'job/intent.json'
        intent = self.run.base.read(path)
        del intent['project_root']
        self.run.replace(path, intent)
        with cwd(root), patch.object(self.run.base, 'manager_environment', return_value=({}, 'fixture')):
            self.assertEqual(self.run.stop(root / 'job')['state'], 'CANCELLED')

    def test_ledger_rejects_foreign_project_binding(self):
        first, second = self.roots
        self.enqueue(first)
        path = first / 'job/intent.json'
        intent = self.run.base.read(path)
        intent['project_root'] = str(second)
        self.run.replace(path, intent)
        with self.assertRaisesRegex(self.run.base.Refused, 'mixed_candidate_or_ledger_custody'):
            self.run.read_ledger(first / 'state')

    def test_cli_preserves_required_state_and_refuses_cross_root(self):
        first, second = self.roots
        args = [sys.executable, '-B', str(HERE / 'runner.py'), 'run', '--job', str(first / 'job'), '--config', 'unused']
        missing = subprocess.run(args, capture_output=True, text=True, cwd=first)
        self.assertEqual(missing.returncode, 2)
        self.assertIn('--state', missing.stderr)
        refused = subprocess.run(args + ['--state', str(second / 'state'), '--project-root', str(first)],
                                 capture_output=True, text=True, cwd=second)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(json.loads(refused.stdout), {'state': 'REFUSED', 'error': 'state_outside_project'})
        self.assertFalse((first / 'job').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
