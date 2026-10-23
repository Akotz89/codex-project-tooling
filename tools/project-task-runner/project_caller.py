"""Generic project-bound caller for maintained task runtimes."""
import argparse
import builtins
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import types

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
BINDING_PATH = None
RUNTIME = None
BOOTSTRAP_API = None
EXPECTED_BINDING_SHA256 = None


def configure(binding_path, caller_api=None, *, expected_binding_sha256=None):
    """Bind one explicit project data document before caller operations."""
    global BINDING_PATH, PROJECT_CONTEXT_PATH, BOOTSTRAP_API, EXPECTED_BINDING_SHA256
    BINDING_PATH = Path(binding_path)
    raw = read_regular(BINDING_PATH, limit=2 * 1024 * 1024)
    value = json.loads(raw)
    if type(value) is not dict or type(value.get('project_context_path')) is not str:
        raise Refused('binding_schema')
    PROJECT_CONTEXT_PATH = Path(value['project_context_path'])
    BOOTSTRAP_API = caller_api
    EXPECTED_BINDING_SHA256 = expected_binding_sha256
    return BINDING_PATH


class Refused(Exception):
    pass


def need(value, code):
    if not value:
        raise Refused(code)


def canonical_project_marker(path, root):
    try:
        resolved = path.resolve(strict=True)
        return (resolved == path and resolved.is_relative_to(root)
                and resolved.is_file())
    except (OSError, RuntimeError):
        return False


def qualification_input_root(value, root):
    """Accept only one canonical, contained project-relative input directory."""
    need(type(value) is str and value and '\\' not in value
         and re.match(r'^[A-Za-z]:', value) is None, 'qualification_root')
    relative = Path(value)
    need(not relative.is_absolute() and str(relative) == value and bool(relative.parts)
         and not any(part in ('', '.', '..') for part in relative.parts),
         'qualification_root')
    path = root / relative
    try:
        resolved = path.resolve(strict=False)
        exists = path.exists()
        is_dir = path.is_dir()
    except (OSError, RuntimeError, ValueError):
        raise Refused('qualification_root')
    need(resolved == path and resolved.is_relative_to(root)
         and (not exists or is_dir), 'qualification_root')
    return path


def read_regular(path, *, private=False, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'binding_file_type')
        if private:
            need(info.st_uid == os.getuid() and not info.st_mode & 0o077, 'private_file')
        raw = source.read(limit + 1)
        need(len(raw) <= limit, 'binding_file_size')
    return raw


def sha256_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, 'source_file_type')
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(source.fileno())
    need((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
         == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
         'source_file_changed')
    return digest.hexdigest()


def binding():
    need(BINDING_PATH is not None, 'binding_not_configured')
    path = BINDING_PATH.resolve(strict=True)
    need(path == BINDING_PATH, 'binding_path')
    binding_raw = read_regular(path)
    binding_sha256 = hashlib.sha256(binding_raw).hexdigest()
    need(type(EXPECTED_BINDING_SHA256) is str
         and re.fullmatch(r'[0-9a-f]{64}', EXPECTED_BINDING_SHA256) is not None
         and binding_sha256 == EXPECTED_BINDING_SHA256, 'root_binding_trust')
    value = json.loads(binding_raw)
    caller_contract = value.get('caller_contract') if type(value) is dict else None
    need(type(caller_contract) is dict, 'caller_contract')
    keys = {'schema', 'project_id', 'project_root', 'tooling_commit',
            'tooling_main_commit',
            'generic_runner_path', 'generic_runner_sha256', 'runtime_path',
            'runtime_runner_sha256', 'canonical_state', 'code_root',
            'qualification_root', 'project_context_path', 'project_context_sha256',
            'caller_api_path', 'caller_api_sha256', 'candidate_adaptive_root',
            'candidate_source_pins', 'candidate_context_api_sha256',
            'caller_path', 'caller_sha256', 'tooling_candidate_root',
            'project_runtime_binding', 'root_packet_contract', 'caller_contract'}
    need(type(value) is dict and set(value) == keys, 'binding_schema')
    caller_path = Path(value['caller_path'])
    need(caller_path.is_absolute() and caller_path.resolve(strict=True) == caller_path
         and caller_path == Path(__file__).resolve(strict=True)
         and re.fullmatch(r'[0-9a-f]{64}', value['caller_sha256']) is not None
         and sha256_file(caller_path) == value['caller_sha256'], 'caller_source_binding')
    need(type(caller_contract.get('binding_schema')) is str
         and value['schema'] == caller_contract['binding_schema'] and type(value['project_id']) is str
         and re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', value['project_id']) is not None,
         'project_identity')
    root = Path(value['project_root'])
    state = Path(value['canonical_state'])
    code_root = Path(value['code_root'])
    agents_marker = root / 'AGENTS.md'
    context_marker = root / '.factory/project-context.md'
    need(root.is_absolute() and root.resolve(strict=True) == root and root.is_dir()
         and canonical_project_marker(agents_marker, root)
         and canonical_project_marker(context_marker, root), 'project_root')
    need(state.is_absolute() and state.resolve(strict=True) == state and state.is_relative_to(root),
         'canonical_state')
    need(code_root.is_absolute() and code_root.resolve(strict=True) == code_root, 'code_root')
    need(re.fullmatch(r'[0-9a-f]{40}', value['tooling_commit']) is not None, 'tooling_commit')
    need(re.fullmatch(r'[0-9a-f]{40}', value['tooling_main_commit']) is not None,
         'published_tooling_main_commit')
    generic_path = Path(value['generic_runner_path'])
    adaptive_path = Path(value['runtime_path']) / 'runner.py'
    history_root = qualification_input_root(value['qualification_root'], root)
    caller_api_path = Path(value['caller_api_path'])
    need(generic_path.is_absolute() and generic_path.resolve(strict=True) == generic_path,
         'generic_runner_path')
    need(adaptive_path.is_absolute() and adaptive_path.resolve(strict=True) == adaptive_path,
         'adaptive_runner_path')
    project_context_path = Path(value['project_context_path'])
    need(project_context_path.is_absolute() and project_context_path.resolve(strict=True) == project_context_path
         and re.fullmatch(r'[0-9a-f]{64}', value['project_context_sha256']) is not None
         and sha256_file(project_context_path) == value['project_context_sha256'],
         'project_context_binding')
    need(caller_api_path.is_absolute() and caller_api_path.resolve(strict=True) == caller_api_path
         and caller_api_path.name == 'caller_api.py', 'caller_api_path')
    need(caller_api_path == code_root / 'tools/project-task-runner/caller_api.py',
         'published_caller_api_binding')
    for name, path_key in (('generic_runner_sha256', generic_path),
                           ('runtime_runner_sha256', adaptive_path),
                           ('caller_api_sha256', caller_api_path)):
        expected = value[name]
        need(re.fullmatch(r'[0-9a-f]{64}', expected) is not None
             and sha256_file(path_key) == expected, 'source_binding_' + name)
    candidate_adaptive = Path(value['candidate_adaptive_root'])
    candidate_root = Path(value['tooling_candidate_root'])
    pins = value['candidate_source_pins']
    need(candidate_adaptive.is_absolute()
         and candidate_adaptive.resolve(strict=True) == candidate_adaptive
         and candidate_adaptive.name == 'adaptive'
         and candidate_adaptive.parent.name == 'project-task-runner'
         and candidate_root.is_absolute() and candidate_root.resolve(strict=True) == candidate_root
         and candidate_adaptive.is_relative_to(candidate_root)
         and type(pins) is dict and pins,
         'candidate_runtime_binding')
    need(re.fullmatch(r'[0-9a-f]{64}', value['candidate_context_api_sha256']) is not None
         and sha256_file(candidate_root / 'tools/project-task-runner/project_context.py')
         == value['candidate_context_api_sha256'], 'candidate_context_api_pin')
    for path, expected in pins.items():
        source_path = Path(path)
        need(source_path.is_absolute() and source_path.resolve(strict=True) == source_path
             and re.fullmatch(r'[0-9a-f]{64}', expected) is not None
             and sha256_file(source_path) == expected, 'candidate_runtime_source_pin')
        if source_path.suffix == '.py':
            need(source_path.is_relative_to(candidate_root), 'candidate_runtime_source_root')
    runtime_binding = value['project_runtime_binding']
    need(type(runtime_binding) is dict and set(runtime_binding) == {
         'project_id', 'unit_prefix', 'payload_environment'}
         and runtime_binding['project_id'] == value['project_id']
         and runtime_binding == value['root_packet_contract'].get('legacy_project_binding'),
         'project_runtime_binding')
    packet_contract = value['root_packet_contract']
    caller_contract = value['caller_contract']
    contract_keys = {'binding_schema', 'runtime_context_sha256', 'source_view_sha256',
                     'selection_schema', 'successor_profile_schema', 'legacy_profile_schema',
                     'source_provenance_schema', 'receipt_schema', 'qualification_source_names',
                     'historical_source_names', 'caller_entry_name', 'runtime_context_name',
                     'source_view_name', 'profile_names', 'promotion_name', 'receipt_name'}
    need(set(caller_contract) == contract_keys and type(caller_contract['binding_schema']) is str
         and type(caller_contract['qualification_source_names']) is list
         and type(caller_contract['historical_source_names']) is list
        and type(caller_contract['profile_names']) is list
        and all(type(caller_contract[key]) is str for key in (
            'runtime_context_sha256', 'source_view_sha256', 'selection_schema',
            'successor_profile_schema', 'legacy_profile_schema',
            'source_provenance_schema', 'receipt_schema', 'caller_entry_name',
            'runtime_context_name', 'source_view_name', 'promotion_name', 'receipt_name'))
        and all(Path(caller_contract[key]).name == caller_contract[key] for key in (
            'caller_entry_name', 'runtime_context_name', 'source_view_name'))
        and all(type(name) is str and Path(name).name == name
                for name in caller_contract['profile_names'])
        and all(type(name) is str and Path(name).name == name
                for name in caller_contract['qualification_source_names']
                + caller_contract['historical_source_names'])
        and set(caller_contract['qualification_source_names']) <= set(
            caller_contract['historical_source_names']), 'caller_contract_schema')
    need(type(packet_contract) is dict
         and type(packet_contract.get('actual_job_authorized')) is bool
         and packet_contract.get('project_root') == str(root)
         and packet_contract.get('canonical_state') == value['canonical_state']
         and packet_contract.get('code_root') == value['code_root']
         and packet_contract.get('runtime_path') == value['runtime_path'],
         'root_packet_contract')
    return value, root, binding_sha256


def load_runtime(value):
    """Load the candidate generic runner for legacy run/status/stop receipts."""
    global RUNTIME
    caller_api = load_caller_api(value)
    runtime = caller_api.load_pinned_module('shared_project_task_runner',
                                            Path(value['candidate_adaptive_root']).parent / 'runner.py',
                                            value['candidate_source_pins'][str(Path(value['candidate_adaptive_root']).parent / 'runner.py')])
    required = ('configuration', 'launch', 'load_job', 'public_status', 'stop')
    need(all(callable(getattr(runtime, name, None)) for name in required), 'tooling_runner_contract')
    RUNTIME = runtime
    return runtime


def load_caller_api(value):
    """Bootstrap the separately pinned generic API from one hashed byte image."""
    path = Path(value['caller_api_path'])
    if BOOTSTRAP_API is not None:
        need(Path(BOOTSTRAP_API.__file__).resolve(strict=True) == path
             and getattr(BOOTSTRAP_API, '__verified_source_sha256__', None)
             == value['caller_api_sha256'], 'published_caller_api_binding')
        return BOOTSTRAP_API
    raw = read_regular(path, limit=2 * 1024 * 1024)
    need(hashlib.sha256(raw).hexdigest() == value['caller_api_sha256'],
         'source_pin_before_execute')
    module = types.ModuleType('shared_caller_api')
    module.__file__ = str(path)
    module.__dict__['__builtins__'] = builtins.__dict__
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    module.__verified_source_sha256__ = hashlib.sha256(raw).hexdigest()
    required = ('read_pinned_source', 'load_pinned_module', 'execute_verified_source')
    need(all(callable(getattr(module, name, None)) for name in required),
         'caller_api_loader_contract')
    return module


def load_selected_context(value, selection):
    """Bind a successor runtime from one exact Root packet triple."""
    need(type(selection) is dict and set(selection) == {
        'schema', 'source_evidence', 'candidate', 'adoption'}
         and selection['schema'] == value['caller_contract']['selection_schema'],
         'successor_context_schema')
    caller_api = load_caller_api(value)
    context = caller_api.load_pinned_module('project_runtime_context',
                                            Path(value['project_context_path']),
                                            value['project_context_sha256'])
    context_api = load_candidate_context_api(value, caller_api)
    need(callable(getattr(context, 'bind_selected_context', None)),
         'successor_context_loader_contract')
    try:
        bound = context.bind_selected_context(
            selection, Path(value['project_root']), caller_api=caller_api,
            project_id=value['project_id'], contract=value['root_packet_contract'],
            context_api=context_api)
    except (OSError, ValueError, TypeError, KeyError, caller_api.Refused) as error:
        raise Refused(str(error)) from error
    need(bound['project_root'] == Path(value['project_root'])
         and bound['canonical_state'] == Path(value['canonical_state'])
         and Path(bound['adoption']['runtime_path']) == Path(value['runtime_path']),
         'successor_context_installed_binding')
    bound['historical_runtime'] = bound['runtime']
    adaptive = bind_candidate_runtime(value, bound, caller_api)
    return context, bound, adaptive


def selected_profile(context, adaptive, bound, config_path, job, config, value,
                     *, require_absent=True, profile_raw=None, profile_path=None,
                     caller_argv=True):
    """Validate a profile and source map against the same selected packet triple."""
    argv = config.get('argv')
    if caller_argv:
        need(type(argv) is list and len(argv) == 7 and argv[1] == '-B'
             and argv[3:5] == ['--job', str(job)] and argv[5] == '--profile',
             'successor_caller_argv')
        caller = Path(bound['project_root']) / value['qualification_root'] / value['caller_contract']['caller_entry_name']
        executable = next((Path(path) for path in bound['candidate']['runtime_source_pins']
                           if Path(path).name == 'python3.14'), None)
        need(executable is not None and argv[0] == str(executable)
             and argv[2] == str(caller) and caller.is_file()
             and config['sources'].get(str(caller)) == adaptive.base.source_digest(caller),
             'successor_caller_binding')
        profile_path = Path(argv[6])
    else:
        need(type(argv) is list and argv and all(type(arg) is str for arg in argv)
             and not root04_preflight(value, config), 'successor_payload_argv')
        executable = Path(argv[0])
        need(executable.is_absolute() and executable.is_file()
             and config.get('sources', {}).get(str(executable))
             == adaptive.base.source_digest(executable), 'successor_payload_binding')
        need(profile_path is not None, 'successor_profile_input')
        profile_path = Path(profile_path)
    need(profile_path.is_absolute() and profile_path.resolve(strict=True) == profile_path
         and profile_path.is_relative_to(bound['project_root']), 'successor_profile_path')
    config_path = Path(config_path)
    need(config_path.is_absolute() and config_path.resolve(strict=True) == config_path
         and config_path.is_relative_to(bound['project_root']), 'successor_config_path')
    if profile_raw is None:
        profile_raw = read_regular(profile_path, private=True)
    profile_hash = hashlib.sha256(profile_raw).hexdigest()
    need(config.get('sources', {}).get(str(profile_path)) == profile_hash,
         'successor_profile_source_pin')
    profile = json.loads(profile_raw)
    fields = {'schema', 'config', 'project_root', 'canonical_state', 'source_map',
              'future_job', 'promoted_source_map', 'artifacts', 'evidence',
              'actual_job_authorized', 'context_selection'}
    need(type(profile) is dict and set(profile) == fields
         and profile['schema'] == value['caller_contract']['successor_profile_schema']
         and profile['config'] == str(config_path)
         and profile['project_root'] == str(bound['project_root'])
         and profile['canonical_state'] == str(bound['canonical_state'])
         and profile['future_job'] == str(job)
         and profile['context_selection'] == bound['context_selection']
         and type(profile['actual_job_authorized']) is bool
         and profile['actual_job_authorized'] is bound['adoption']['actual_job_authorized'] is True,
         'successor_profile_binding')
    for key in ('source_evidence', 'candidate', 'adoption'):
        reference = bound['context_selection'][key]
        need(config.get('sources', {}).get(reference['path']) == reference['sha256'],
             'successor_context_source_pin')
    reference = profile['source_map']
    need(type(reference) is dict and set(reference) == {'path', 'sha256', 'source_identity'},
         'successor_source_map_reference')
    source_path = Path(reference['path'])
    need(source_path.is_absolute() and source_path.resolve(strict=True) == source_path
         and source_path.is_relative_to(bound['project_root']), 'successor_source_map_path')
    source_raw = read_regular(source_path, private=True, limit=1048576)
    source_digest = hashlib.sha256(source_raw).hexdigest()
    need(source_digest == reference['sha256']
         and config.get('sources', {}).get(str(source_path)) == source_digest,
         'successor_source_map_hash')
    source_map = json.loads(source_raw)
    source_fields = {'schema', 'project_root', 'canonical_state', 'code_root',
                     'maintained_main', 'root_packets', 'runtime_source_pins',
                     'accounting', 'local_source_pins', 'published_tooling', 'source_identity',
                     'estimates_are_forecasts_only', 'actual_job_authorized'}
    expected_packets = {key: bound['context_selection'][key]
                        for key in ('source_evidence', 'candidate', 'adoption')}
    need(type(source_map) is dict and set(source_map) == source_fields
         and source_map['schema'] == value['caller_contract']['source_provenance_schema']
         and source_map['project_root'] == str(bound['project_root'])
         and source_map['canonical_state'] == str(bound['canonical_state'])
         and source_map['code_root'] == bound['adoption']['code_root']
         and source_map['maintained_main'] == bound['source_evidence']['source']['observed_merge']
         and source_map['root_packets'] == expected_packets
         and source_map['runtime_source_pins'] == bound['candidate']['runtime_source_pins']
         and source_map['accounting'] == {
             'accounting_reader': bound['accounting_reader'],
             'capacity_guard': bound['capacity_guard']}
         and source_map['published_tooling'] == {
             'commit': value['tooling_commit'],
             'caller_api_path': value['caller_api_path'],
             'caller_api_sha256': value['caller_api_sha256']}
         and source_map['estimates_are_forecasts_only'] is True
         and type(source_map['actual_job_authorized']) is bool
         and source_map['actual_job_authorized'] is bound['adoption']['actual_job_authorized'] is True,
         'successor_source_map_binding')
    history_root = Path(bound['project_root']) / value['qualification_root']
    qualification_names = set(value['caller_contract']['qualification_source_names'])
    historical_names = set(value['caller_contract']['historical_source_names'])
    need(qualification_names <= historical_names, 'qualification_source_contract')
    local_names = historical_names
    local_pins = source_map['local_source_pins']
    api_path = str(Path(value['caller_api_path']).resolve(strict=True))
    expected_local_names = local_names | {'caller_api.py'}
    local_by_name = {}
    if type(local_pins) is dict:
        for path, digest in local_pins.items():
            need(type(path) is str, 'successor_local_source_membership')
            source_path = Path(path)
            need(source_path.is_absolute()
                 and os.path.abspath(path) == path
                 and type(digest) is str
                 and re.fullmatch(r'[0-9a-f]{64}', digest) is not None
                 and source_path.name not in local_by_name,
                 'successor_local_source_membership')
            local_by_name[source_path.name] = (path, digest)
    need(type(local_pins) is dict and set(local_by_name) == expected_local_names
         and local_by_name[value['caller_contract']['runtime_context_name']][0] == str(history_root / value['caller_contract']['runtime_context_name'])
         and local_by_name['caller_api.py'] == (api_path, value['caller_api_sha256']),
         'successor_local_source_membership')
    pins = dict(source_map['runtime_source_pins']) | dict(local_pins)
    for key in ('accounting_reader', 'capacity_guard'):
        item = source_map['accounting'][key]
        pins[item['path']] = item['sha256']
    history_paths = {local_by_name[name][0] for name in historical_names}
    if caller_argv:
        expected_paths = {str(history_root / name) for name in historical_names}
        expected_paths.add(api_path)
        need(set(local_pins) == expected_paths, 'successor_local_source_membership')
        checked_pins = pins
    else:
        # These hashes remain in the source identity as Root04 qualification
        # history. They are not ordinary-v2 execution dependencies and must
        # not force those task-local files to exist at launch/status/stop.
        need(all(Path(path).is_relative_to(bound['project_root'] / 'work')
                 for path in history_paths), 'qualification_source_history_path')
        checked_pins = dict(source_map['runtime_source_pins'])
        checked_pins[api_path] = value['caller_api_sha256']
        checked_pins.update({item['path']: item['sha256']
                             for item in source_map['accounting'].values()})
    checked_pins[str(Path(value['project_context_path']))] = value['project_context_sha256']
    adaptive.base.pins(checked_pins)
    source_id = hashlib.sha256(json.dumps(
        {key: item for key, item in source_map.items() if key != 'source_identity'},
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    need(source_id == source_map['source_identity'] == reference['source_identity'],
         'successor_source_identity')
    for path, digest in checked_pins.items():
        need(config.get('sources', {}).get(path) == digest, 'successor_source_not_exported')
    need(profile['promoted_source_map'] == str(Path(profile['artifacts']) / value['caller_contract']['promotion_name'])
         and profile['artifacts'] == str(job / 'artifacts')
         and profile['evidence'] == str(job / 'evidence'), 'successor_promotion_binding')
    if require_absent:
        need(not any(Path(profile[key]).exists() for key in ('future_job', 'artifacts', 'evidence')),
             'future_job_must_remain_absent')
    return profile, {'source_identity': source_id, 'source_map': source_map,
                     'pins': pins, 'project_root': bound['project_root'],
                     'canonical_state': bound['canonical_state']}


def successor_profile_input(config, root, value):
    """Find one exact-pinned successor profile among the v2 config inputs."""
    inputs = config.get('inputs')
    sources = config.get('sources')
    if inputs is None and sources is None:
        return None
    need(type(inputs) is list and type(sources) is dict,
         'successor_profile_inputs_shape')
    matches = []
    for raw_path in inputs:
        need(type(raw_path) is str, 'successor_profile_input_path')
        if Path(raw_path).name not in tuple(value['caller_contract']['profile_names']):
            continue
        path = project_path(raw_path, root, 'successor_profile_path', must_exist=True)
        raw = read_regular(path, private=True)
        expected = sources.get(str(path))
        need(type(expected) is str and hashlib.sha256(raw).hexdigest() == expected,
             'successor_profile_source_pin')
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Refused('successor_profile_shape') from error
        need(type(document) is dict, 'successor_profile_shape')
        if document.get('schema') == value['caller_contract']['successor_profile_schema']:
            matches.append((path, raw, document))
    need(len(matches) <= 1, 'successor_profile_ambiguous')
    return matches[0] if matches else None


def caller_profile_document(config, root, job, value):
    """Read a profile only after its caller-config source pin is checked."""
    argv = config.get('argv')
    need(type(argv) is list and len(argv) == 7 and argv[3:5] == ['--job', str(job)]
         and argv[5] == '--profile', 'root04_caller_argv')
    path = project_path(argv[6], root, 'root04_profile_path', must_exist=True)
    raw = read_regular(path, private=True)
    expected = config.get('sources', {}).get(str(path))
    need(type(expected) is str and hashlib.sha256(raw).hexdigest() == expected,
         'root04_profile_source_pin')
    document = json.loads(raw)
    need(type(document) is dict, 'root04_profile_shape')
    return path, raw, document


def load_root_runtime(value):
    """Bind the installed project context and exact Root04 packet state."""
    global RUNTIME
    caller_api = load_caller_api(value)
    context_path = Path(value['project_context_path'])
    context = caller_api.load_pinned_module('project_runtime_context', context_path,
                                            value['project_context_sha256'])
    context_api = load_candidate_context_api(value, caller_api)
    # Read the immutable Root packet pins before runtime_context imports any
    # adaptive code. Each dynamic module is then compiled from these exact bytes.
    try:
        packets, references = context.read_root_packets(
            Path(value['project_root']), contract=value['root_packet_contract'],
            caller_api=caller_api, context_api=context_api)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise Refused('root04_installed_binding') from error
    evidence = packets['source_evidence']
    candidate = packets['candidate']
    adoption = packets['adoption']
    pins = candidate['runtime_source_pins']
    need(evidence['runtime_source_pins'] == pins
         and adoption['runtime_source_pins'] == pins, 'root04_packet_pins')
    bound = context.bind_runtime(Path(value['project_root']), caller_api=caller_api,
                                 project_id=value['project_id'], packets=packets,
                                 references=references,
                                 contract=value['root_packet_contract'],
                                 context_api=context_api)
    adaptive = bound['runtime']
    adoption = bound['adoption']
    need(bound['project_root'] == Path(value['project_root'])
         and bound['canonical_state'] == Path(value['canonical_state'])
         and Path(adaptive.CODE_ROOT) == Path(value['code_root'])
         and Path(adoption['runtime_path']) == Path(value['runtime_path'])
         and Path(adaptive.__file__).resolve() == Path(value['runtime_path']) / 'runner.py'
         and adaptive.__verified_source_sha256__ == value['runtime_runner_sha256'],
         'root04_installed_binding')
    need(adoption['actual_runtime_adoption_authorized'] is True,
         'root04_runtime_adoption_not_authorized')
    # The Root packet proves the maintained published runtime identity above.
    # Candidate execution uses the same generic API with a separate exact-byte
    # source map; project packet validation remains in the project adapter.
    bound['historical_runtime'] = adaptive
    candidate_adaptive = bind_candidate_runtime(value, bound, caller_api)
    RUNTIME = candidate_adaptive.base
    return context, bound, candidate_adaptive


def load_candidate_adaptive(value, caller_api=None):
    """Execute the isolated generic runtime from the exact pinned candidate map."""
    caller_api = caller_api or load_caller_api(value)
    adaptive_root = Path(value['candidate_adaptive_root'])
    pins = value['candidate_source_pins']
    verified = {}
    for raw_path, expected in pins.items():
        path = Path(raw_path)
        if path.suffix == '.py':
            verified[path] = caller_api.read_pinned_source(path, expected)
    runner_path = adaptive_root / 'runner.py'
    raw = verified.get(runner_path)
    need(raw is not None, 'candidate_runtime_runner_pin')
    return caller_api.execute_verified_source(
        'candidate_adaptive_runtime', runner_path, raw,
        source_bytes=verified, trusted_root=adaptive_root.parents[2])


def bind_candidate_runtime(value, bound, caller_api):
    runtime = load_candidate_adaptive(value, caller_api)
    state = runtime.bind_context(bound['project_root'], bound['canonical_state'],
                                 value['project_id'])
    need(state == bound['canonical_state']
         and Path(runtime.CODE_ROOT) == Path(value['candidate_adaptive_root']).parents[2]
         and runtime.source_pins() == value['candidate_source_pins'],
         'candidate_runtime_binding')
    bound['candidate_runtime_root'] = Path(value['candidate_adaptive_root'])
    return runtime


def load_candidate_context_api(value, caller_api):
    path = Path(value['candidate_adaptive_root']).parents[2] / 'tools/project-task-runner/project_context.py'
    expected = value['candidate_context_api_sha256']
    raw = caller_api.read_pinned_source(path, expected)
    return caller_api.execute_verified_source(
        'generic_project_context_api', path, raw,
        source_bytes={path: raw}, trusted_root=Path(value['candidate_adaptive_root']).parents[2])


def project_path(value, root, code, *, must_exist):
    path = Path(value).expanduser()
    need(path.is_absolute(), code)
    try:
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError):
        raise Refused(code)
    need(resolved != root and resolved.is_relative_to(root), code)
    return resolved


def bind_new_legacy_config(runtime, config_path, root, binding_path, binding_sha256):
    config = project_path(config_path, root, 'config_outside_project', must_exist=True)
    value, _ = runtime.configuration(config)
    cwd = project_path(value['cwd'], root, 'cwd_outside_project', must_exist=True)
    need(cwd.is_dir(), 'cwd_not_directory')
    need(value['sources'].get(str(binding_path)) == binding_sha256, 'project_binding_source_pin')
    return config


def root04_preflight(value, config):
    """Identify the exact Root04 caller/profile entrypoint, not every v2 job."""
    argv = config.get('argv')
    if type(argv) is not list or len(argv) != 7:
        return False
    history_root = Path(value['project_root']) / value['qualification_root']
    caller = history_root / value['caller_contract']['caller_entry_name']
    return (argv[1] == '-B' and argv[2] == str(caller) and argv[3] == '--job'
            and argv[5] == '--profile'
            and Path(argv[6]).name in tuple(value['caller_contract']['profile_names']))


def root04_profile(context, adaptive, bound, config_path, job, config, value,
                   *, require_absent=True, profile_raw=None):
    """Verify the v2 caller/profile/export map before any adaptive admission."""
    need('context_selection' not in bound, 'root04_context_mismatch')
    argv = config['argv']
    need(len(argv) == 7 and argv[1] == '-B' and argv[3:5] == ['--job', str(job)]
         and argv[5] == '--profile', 'root04_caller_argv')
    executable = next((Path(p) for p in bound['candidate']['runtime_source_pins']
                       if Path(p).name == 'python3.14'), None)
    history_root = Path(bound['project_root']) / value['qualification_root']
    task_root = history_root.parent
    context_path = history_root / value['caller_contract']['runtime_context_name']
    caller = history_root / value['caller_contract']['caller_entry_name']
    profile_path = Path(argv[6])
    need(executable is not None and argv[0] == str(executable)
         and argv[2] == str(caller) and caller.is_file(), 'root04_caller_binding')
    base = adaptive.base
    need(config['sources'].get(str(caller)) == base.source_digest(caller), 'root04_caller_source_pin')
    need(profile_path.is_absolute() and profile_path == profile_path.resolve(strict=True),
         'root04_profile_path')
    if profile_raw is None:
        profile_raw = base.read_bytes(profile_path)
    profile_digest = base.digest(profile_raw)
    need(config['sources'].get(str(profile_path)) == profile_digest,
         'root04_profile_source_pin')
    profile = json.loads(profile_raw)
    fields = {'schema', 'config', 'project_root', 'canonical_state', 'source_map',
              'future_job', 'promoted_source_map', 'artifacts', 'evidence',
              'actual_job_authorized'}
    need(type(profile) is dict and set(profile) == fields
         and profile['schema'] == value['caller_contract']['legacy_profile_schema']
         and profile['config'] == str(config_path)
         and profile['project_root'] == str(bound['project_root'])
         and profile['canonical_state'] == str(bound['canonical_state'])
         and profile['future_job'] == str(job)
         and type(profile['actual_job_authorized']) is bool
         and profile['actual_job_authorized'] == bound['adoption']['actual_job_authorized'],
         'root04_profile_binding')
    provenance = profile['source_map']
    need(type(provenance) is dict and set(provenance) == {'path', 'sha256', 'source_identity'}
         and type(provenance['path']) is str
         and re.fullmatch(r'[0-9a-f]{64}', provenance['sha256']) is not None
         and re.fullmatch(r'[0-9a-f]{64}', provenance['source_identity']) is not None,
         'source_provenance_reference')
    provenance_path = Path(provenance['path'])
    need(provenance_path.is_absolute()
         and provenance_path == provenance_path.resolve(strict=True)
         and provenance_path.is_relative_to(bound['project_root'] / 'work')
         and config['sources'].get(str(provenance_path)) == provenance['sha256'],
         'source_provenance_path')
    provenance_raw = read_regular(provenance_path, private=True, limit=1048576)
    need(hashlib.sha256(provenance_raw).hexdigest() == provenance['sha256'],
         'source_provenance_hash')
    provenance_document = json.loads(provenance_raw)
    need(type(provenance_document) is dict
         and provenance_document.get('schema') == value['caller_contract']['source_provenance_schema']
         and type(provenance_document.get('local_source_pins')) is dict,
         'source_provenance_shape')
    source_view_path = (history_root / value['caller_contract']['source_view_name']).resolve(strict=True)
    expected_view = config['sources'].get(str(source_view_path))
    need(expected_view == value['caller_contract']['source_view_sha256']
         and provenance_document.get('local_source_pins', {}).get(str(source_view_path)) == expected_view,
         'source_view_pin_binding')
    caller_api = load_caller_api(value)
    source_view_raw = caller_api.read_pinned_source(source_view_path, expected_view)
    historical_context_raw = caller_api.read_pinned_source(
        context_path, value['caller_contract']['runtime_context_sha256'])
    context_path = context_path.resolve(strict=True)
    source_view = caller_api.execute_verified_source(
        'project_source_view', source_view_path, source_view_raw,
        source_bytes={source_view_path: source_view_raw,
                      context_path: historical_context_raw},
        trusted_root=task_root / 'source')
    source = source_view.inspect(profile['source_map'], root=bound['project_root'], bound=bound)
    need(source['source_identity'] == profile['source_map']['source_identity'], 'source_identity_binding')
    for path, digest in source['pins'].items():
        need(config['sources'].get(path) == digest, 'source_map_not_exported')
    need(profile['promoted_source_map'] == str(Path(profile['artifacts']) / source_view.PROMOTION_NAME)
         and profile['artifacts'] == str(job / 'artifacts')
         and profile['evidence'] == str(job / 'evidence'), 'promotion_path_binding')
    if require_absent:
        need(not any(Path(profile[key]).exists() for key in ('future_job', 'artifacts', 'evidence')),
             'future_job_must_remain_absent')
    return profile, source


def root04_existing_profile(context, adaptive, bound, job, config, value):
    """Revalidate the project-bound profile after the future job exists."""
    selected = successor_profile_input(config, bound['project_root'], value)
    if selected is not None:
        profile_path, raw, document = selected
        need(bound.get('context_selection') == document.get('context_selection'),
             'successor_context_cross_binding')
        config_path = project_path(document.get('config'), bound['project_root'],
                                   'successor_config_path', must_exist=True)
        parsed, _ = adaptive.configuration(config_path)
        need(parsed == config, 'successor_config_reopen_binding')
        return selected_profile(context, adaptive, bound, config_path, job, parsed, value,
                                require_absent=False, profile_raw=raw,
                                profile_path=profile_path, caller_argv=False)
    profile_path, raw, document = caller_profile_document(config, bound['project_root'], job, value)
    config_path = Path(document.get('config', ''))
    if document.get('schema') == value['caller_contract']['successor_profile_schema']:
        need(bound.get('context_selection') == document.get('context_selection'),
             'successor_context_cross_binding')
        return selected_profile(context, adaptive, bound, config_path, job, config, value,
                                require_absent=False, profile_raw=raw)
    need('context_selection' not in bound, 'successor_context_cross_binding')
    return root04_profile(context, adaptive, bound, config_path, job, config, value,
                          require_absent=False, profile_raw=raw)


def consume_root04_terminal(value, generic, adaptive, context, bound, job, config):
    """Consume a complete runtime receipt only after adaptive status is terminal."""
    profile, source = root04_existing_profile(context, adaptive, bound, job, config, value)
    artifacts = Path(profile['artifacts'])
    evidence = Path(profile['evidence'])
    outcome_path = evidence / 'outcome.json'
    outcome_raw = generic.read_bytes(outcome_path)
    outcome = json.loads(outcome_raw)
    need(type(outcome) is dict, 'terminal_outcome_shape')
    if outcome.get('state') != 'complete':
        return {'state': outcome.get('state'), 'complete': False,
                'outcome_path': str(outcome_path)}
    caller_api = load_caller_api(value)
    capsule_path = artifacts / value['caller_contract']['receipt_name']
    receipt = caller_api.consume_runtime_promotion(
        outcome_path=outcome_path, promoted_path=capsule_path,
        expected_artifacts=artifacts,
        expected_relative=value['caller_contract']['receipt_name'],
        private_directory=generic.private_dir, read_private=generic.read_bytes,
        digest=generic.digest)
    capsule = json.loads(receipt['promoted_bytes'])
    expected_capsule = {
        'schema': value['caller_contract']['receipt_schema'],
        'project_root': str(bound['project_root']),
        'canonical_state': str(bound['canonical_state']),
        'code_root': str(Path(adaptive.CODE_ROOT)),
        'maintained_main': bound['source_evidence']['source']['observed_merge'],
        'root_packets': source['source_map']['root_packets'],
        'source_identity': source['source_identity'],
        'runtime_source_pins': bound['candidate']['runtime_source_pins'],
        'accounting': {'accounting_reader': bound['accounting_reader'],
                       'capacity_guard': bound['capacity_guard']},
        'local_source_pins': source['source_map']['local_source_pins'],
        'actual_job_authorized': bound['adoption']['actual_job_authorized'],
        'actual_runtime_adoption_authorized': True,
    }
    need(type(capsule) is dict and capsule == expected_capsule,
         'runtime_adoption_receipt_binding')
    return {'complete': True, 'terminal_state': receipt['terminal_state'],
            'scratch_cleanup': receipt['scratch_cleanup'],
            'manifest': receipt['manifest'],
            'promoted_sha256': receipt['promoted_sha256'],
            'source_identity': source['source_identity']}


def dispatch(operation, job_arg, config_arg=None):
    value, root, binding_sha256 = binding()
    generic = load_runtime(value)
    job = project_path(job_arg, root, 'job_outside_project', must_exist=operation != 'run')
    if operation == 'run':
        config_path = project_path(config_arg, root, 'config_outside_project', must_exist=True)
        raw = read_regular(config_path, private=True, limit=1048576)
        document = json.loads(raw)
        if type(document) is dict and document.get('version') == 2:
            context, bound, adaptive = load_root_runtime(value)
            parsed, config_digest = adaptive.configuration(config_path)
            need(parsed == document, 'root04_config_reopen_binding')
            if root04_preflight(value, parsed):
                _profile_path, profile_raw, profile_document = caller_profile_document(
                    parsed, root, job, value)
                if profile_document.get('schema') == value['caller_contract']['successor_profile_schema']:
                    context, bound, adaptive = load_selected_context(
                        value, profile_document.get('context_selection'))
                    selected_config, _ = adaptive.configuration(config_path)
                    need(selected_config == parsed, 'successor_config_reopen_binding')
                    profile, _source = selected_profile(
                        context, adaptive, bound, config_path, job, parsed, value,
                        profile_raw=profile_raw)
                else:
                    need('context_selection' not in bound, 'successor_context_cross_binding')
                    profile, _source = root04_profile(context, adaptive, bound, config_path,
                                                       job, parsed, value,
                                                       profile_raw=profile_raw)
                need(profile['actual_job_authorized'] is True
                     and bound['adoption']['actual_job_authorized'] is True,
                     'actual_job_unauthorized')
            else:
                selected = successor_profile_input(parsed, root, value)
                if selected is not None:
                    profile_path, profile_raw, profile_document = selected
                    context, bound, adaptive = load_selected_context(
                        value, profile_document.get('context_selection'))
                    selected_config, _ = adaptive.configuration(config_path)
                    need(selected_config == parsed, 'successor_config_reopen_binding')
                    profile, _source = selected_profile(
                        context, adaptive, bound, config_path, job, parsed, value,
                        profile_raw=profile_raw, profile_path=profile_path,
                        caller_argv=False)
                else:
                    cwd = project_path(parsed['cwd'], root, 'cwd_outside_project', must_exist=True)
                    need(cwd.is_dir() and bound['canonical_state'] == Path(value['canonical_state']),
                         'adaptive_project_state_binding')
                    need(bound['adoption']['actual_job_authorized'] is True,
                         'actual_job_unauthorized')
            return adaptive.launch(job, config_path, bound['canonical_state'], bound['project_root'],
                                   project_binding=value['project_runtime_binding'])
        config = bind_new_legacy_config(generic, config_path, root, BINDING_PATH.resolve(),
                                        binding_sha256)
        return generic.launch(job, config)
    job, intent = generic.load_job(job)
    if intent.get('contract_version') == 2:
        context, bound, adaptive = load_root_runtime(value)
        selected = successor_profile_input(intent.get('config', {}), root, value)
        if selected is not None:
            profile_path, profile_raw, profile_document = selected
            context, bound, adaptive = load_selected_context(
                value, profile_document.get('context_selection'))
            config_path = project_path(profile_document.get('config'), root,
                                       'successor_config_path', must_exist=True)
            selected_config, _ = adaptive.configuration(config_path)
            need(selected_config == intent['config'], 'successor_config_reopen_binding')
            selected_profile(context, adaptive, bound, config_path, job, selected_config, value,
                             require_absent=False, profile_raw=profile_raw,
                             profile_path=profile_path, caller_argv=False)
            need(intent.get('candidate') == str(bound['adoption']['runtime_path'])
                 and intent.get('state_dir') == str(bound['canonical_state'])
                 and intent.get('project_root') == str(bound['project_root']),
                 'successor_job_binding')
            if operation == 'status':
                result = adaptive.advance(job)
                profile, _source = root04_existing_profile(
                    context, adaptive, bound, job, intent['config'], value)
                outcome_path = Path(profile['evidence']) / 'outcome.json'
                if outcome_path.exists():
                    result = dict(result)
                    result['runtime_promotion'] = consume_root04_terminal(
                        value, generic, adaptive, context, bound, job, intent['config'])
                return result
            return adaptive.stop(job)
        need(intent.get('candidate') == str(bound['adoption']['runtime_path'])
             and intent.get('state_dir') == str(bound['canonical_state'])
             and intent.get('project_root') == str(bound['project_root']),
             'root04_historical_job_binding')
        if operation == 'status':
            if root04_preflight(value, intent.get('config', {})):
                _profile_path, profile_raw, profile_document = caller_profile_document(
                    intent['config'], root, job, value)
                if profile_document.get('schema') == value['caller_contract']['successor_profile_schema']:
                    context, bound, adaptive = load_selected_context(
                        value, profile_document.get('context_selection'))
                    config_path = Path(profile_document['config'])
                    selected_config, _ = adaptive.configuration(config_path)
                    need(selected_config == intent['config'], 'successor_config_reopen_binding')
                    selected_profile(context, adaptive, bound, config_path, job,
                                     selected_config, value, require_absent=False,
                                     profile_raw=profile_raw)
                else:
                    need('context_selection' not in bound, 'successor_context_cross_binding')
                    config_path = project_path(profile_document.get('config'), root,
                                               'root04_config_path', must_exist=True)
                    selected_config, _ = adaptive.configuration(config_path)
                    need(selected_config == intent['config'], 'root04_config_reopen_binding')
                    root04_profile(context, adaptive, bound, config_path, job,
                                   selected_config, value, require_absent=False,
                                   profile_raw=profile_raw)
            result = adaptive.advance(job)
            if root04_preflight(value, intent.get('config', {})):
                profile_document, _source = root04_existing_profile(
                    context, adaptive, bound, job, intent['config'], value)
                outcome_path = Path(profile_document['evidence']) / 'outcome.json'
                if outcome_path.exists():
                    result = dict(result)
                    result['runtime_promotion'] = consume_root04_terminal(
                        value, generic, adaptive, context, bound, job,
                        intent['config'])
            return result
        if root04_preflight(value, intent.get('config', {})):
            _profile_path, profile_raw, profile_document = caller_profile_document(
                intent['config'], root, job, value)
            if profile_document.get('schema') == value['caller_contract']['successor_profile_schema']:
                context, bound, adaptive = load_selected_context(
                    value, profile_document.get('context_selection'))
                config_path = project_path(profile_document.get('config'), root,
                                           'successor_config_path', must_exist=True)
                selected_config, _ = adaptive.configuration(config_path)
                need(selected_config == intent['config'], 'successor_config_reopen_binding')
                selected_profile(context, adaptive, bound, config_path, job, selected_config, value,
                                 require_absent=False, profile_raw=profile_raw)
            else:
                need('context_selection' not in bound, 'successor_context_cross_binding')
                config_path = project_path(profile_document.get('config'), root,
                                           'root04_config_path', must_exist=True)
                selected_config, _ = adaptive.configuration(config_path)
                need(selected_config == intent['config'], 'root04_config_reopen_binding')
                root04_profile(context, adaptive, bound, config_path, job,
                               selected_config, value, require_absent=False,
                               profile_raw=profile_raw)
        return adaptive.stop(job)
    sources = intent.get('config', {}).get('sources', {})
    saved_binding = sources.get(str(BINDING_PATH.resolve()))
    need(saved_binding in (None, binding_sha256), 'historical_binding_changed')
    if operation == 'status':
        return generic.public_status(job)
    return generic.stop(job)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    for name in ('run', 'status', 'stop'):
        command = sub.add_parser(name)
        command.add_argument('--job', required=True)
        if name == 'run':
            command.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        result = dispatch(args.operation, args.job, getattr(args, 'config', None))
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        refused = isinstance(error, Refused) or (RUNTIME is not None and isinstance(error, RUNTIME.Refused))
        state = 'UNKNOWN' if args.operation != 'run' else 'REFUSED'
        print(json.dumps({'state': state, 'error': str(error) if refused else type(error).__name__}, sort_keys=True))
        return 2
