"""Prepare native requests only; the original caller owns dispatch and acceptance."""
from pathlib import Path
import argparse
from datetime import datetime
from datetime import timezone
import hashlib
import json
import math
import os
import re
import sys
import stat
import tempfile
import time
import uuid
import unicodedata
from .jev_router import (choose, http_choice, read_credential, ModelPolicyError,
                        SPAWN_AGENT_EFFORTS, collaboration_capability_pairs, assert_ordinary, eligible,
                        assert_actual_assessment, validate_allowed_models, validate_allowed_efforts,
                        validate_saved_routing_policy)

MAX_CONTEXT = 1024 * 1024


def spawn_agent_request(pair, receipt):
    if not isinstance(pair, dict):
        return None
    model, effort = pair.get('model'), pair.get('effort')
    if model not in SPAWN_AGENT_EFFORTS or effort not in SPAWN_AGENT_EFFORTS[model]:
        return None
    if (receipt.get('capability_provenance') != 'caller_supplied_tool_metadata'
            or receipt.get('capability_tool') != 'collaboration.spawn_agent'):
        return None
    pairs = receipt.get('spawn_capability_pairs')
    if not isinstance(pairs, list) or not any(
            isinstance(row, dict) and row.get('model') == model and row.get('effort') == effort
            for row in pairs):
        return None
    try:
        assert_actual_assessment(receipt, model, effort)
    except (ValueError, TypeError, AttributeError):
        return None
    return {'model': model, 'reasoning_effort': effort, 'fork_turns': 'none'}


def compact_dispatch_receipt(args, status, receipt):
    if args is None:
        return None
    return {'schema': 'CPI236SPAWNREQUESTV1', 'status': status,
            'tool': 'collaboration.spawn_agent', 'arguments': args,
            'dispatch': 'not_invoked', 'reason': receipt.get('reason'),
            'assessment_reason': receipt.get('assessment_reason'),
            'certainty_features_used': receipt.get('certainty_features_used'),
            'capability_provenance': receipt.get('capability_provenance'),
            'capability_tool': receipt.get('capability_tool'),
            'usage': receipt.get('usage', {})}



MAX_INSTRUCTION_REFERENCE = 256 * 1024


def checked_instruction_reference(path, quote):
    """Check bounded source bytes; never retain or log the file or IO error text."""
    try:
        reference = Path(path)
        if (not reference.is_absolute()
                or any(part.is_symlink() for part in (reference, *reference.parents))):
            raise ValueError('reference path')
        fd = os.open(reference, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                     getattr(os, 'O_NONBLOCK', 0))
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o444
                    or before.st_size > MAX_INSTRUCTION_REFERENCE
                    or os.name != 'nt' and before.st_mode & 0o022
                    or hasattr(os, 'getuid') and before.st_uid not in {os.getuid(), 0}):
                raise ValueError('reference file')
            with os.fdopen(fd, 'rb') as stream:
                fd = None
                raw = stream.read(MAX_INSTRUCTION_REFERENCE + 1)
                after = os.fstat(stream.fileno())
            if (len(raw) > MAX_INSTRUCTION_REFERENCE
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                       (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or quote not in raw.decode('utf8')):
                raise ValueError('reference quote or bytes')
        finally:
            if fd is not None:
                os.close(fd)
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ModelPolicyError('safe readable governing instruction with exact quote required') from None

def human_override(choice):
    """Check file-source containment; chat attribution and human authorship remain caller-checked."""
    fields = {'model', 'effort', 'source', 'instruction_quote', 'instruction_reference'}
    if not isinstance(choice, dict) or set(choice) != fields or choice.get('source') != 'direct_human_instruction':
        raise ModelPolicyError('human override attribution required')
    quote = choice['instruction_quote']
    if not isinstance(quote, str) or not quote.strip() or len(quote.encode('utf8')) > 8192:
        raise ModelPolicyError('human override exact instruction quote required')
    reference = choice['instruction_reference']
    if not isinstance(reference, dict):
        raise ModelPolicyError('human override reference required')
    if reference.get('kind') == 'chat_turn':
        if set(reference) != {'kind', 'chat_id', 'turn_id'} or not all(_identifier(reference[k]) for k in ('chat_id', 'turn_id')):
            raise ModelPolicyError('resolvable chat/turn reference required')
    elif reference.get('kind') == 'governing_instruction':
        path = reference.get('path')
        if set(reference) != {'kind', 'path'} or not isinstance(path, str):
            raise ModelPolicyError('resolvable governing instruction reference required')
        checked_instruction_reference(path, quote)
    else:
        raise ModelPolicyError('human override reference kind refused')
    return {'source': choice['source'], 'instruction_quote': quote, 'instruction_reference': dict(reference)}



def route(request, root, key_file=None, jev_model='jev-latest', *, allowed_models=None,
          allowed_efforts=None, saved_routing_policy=None, transport=None, max_task_bytes=8192):
    """Pure preparation core, compatible with Archon's separate native preflight.

    This library does not attest a project or persist evidence. Public commands
    validate read_settings first; collaboration callers use prepare_subagent.
    """
    transport = http_choice if transport is None else transport
    started = time.monotonic()
    receipt = {'schema': 'CPI236LIMITEDV1', 'status': 'fallback',
               'reason': 'native_context_unavailable', 'attempts': 0}
    pair = None
    capability = None
    try:
        if not isinstance(request, dict):
            raise ValueError('native context')
        capability = request.get('capability_provenance')
        if allowed_models is not None:
            allowed_models = validate_allowed_models(allowed_models)
        if allowed_efforts is not None:
            allowed_efforts = validate_allowed_efforts(allowed_efforts)
        if saved_routing_policy is not None:
            saved_routing_policy = validate_saved_routing_policy(saved_routing_policy)
        capability_pairs = (collaboration_capability_pairs(capability) if capability is not None
                            else eligible(request.get('catalog'), time.time()))
        if saved_routing_policy is not None and not any(
                (row['model'], row['effort']) == (saved_routing_policy['model'], saved_routing_policy['effort'])
                and (allowed_models is None or row['model'] in allowed_models)
                and (allowed_efforts is None or row['effort'] in allowed_efforts.get(row['model'], []))
                for row in capability_pairs.values()):
            raise ModelPolicyError('saved routing pair is unavailable or outside project ceilings')
        choice = request.get('explicit_choice')
        attribution = human_override(choice) if choice is not None else None
        if choice is not None and not any(
                (row['model'], row['effort']) == (choice.get('model'), choice.get('effort'))
                for row in capability_pairs.values()
                if (allowed_models is None or row['model'] in allowed_models)
                and (allowed_efforts is None or row['effort'] in allowed_efforts.get(row['model'], []))
                and (saved_routing_policy is None or (row['model'], row['effort']) ==
                     (saved_routing_policy['model'], saved_routing_policy['effort']))):
            raise ModelPolicyError('human pair unavailable or forbidden')
        remaining, task, ordinary = request.get('remaining_ms'), request['task'], request['ordinary']
        if (remaining is not None and
                (isinstance(remaining, bool) or not isinstance(remaining, (int, float))
                or not math.isfinite(remaining) or remaining <= 0)
                or not isinstance(task, str)):
            raise ValueError('native context')
        if capability is not None and 'catalog' in request:
            raise ValueError('choose one capability provenance source')
        deadline = started + remaining / 1000 if remaining is not None else None
        if attribution is not None:
            task += '\nHuman pair constraint: ' + json.dumps(choice, sort_keys=True)
        # Validate ordinary policy/capability before credential IO; ordinary is context only.
        _, receipt = choose(task, request.get('catalog'), None, jev_model, deadline,
                            ordinary=ordinary, capability_provenance=capability, allowed_models=allowed_models,
                            allowed_efforts=allowed_efforts, saved_routing_policy=saved_routing_policy,
                            transport=transport,
                            max_task_bytes=max_task_bytes)
        if 'ordinary_worker' in receipt:
            try:
                key = read_credential(key_file)
            except Exception:
                receipt['reason'] = 'credential_unavailable'
            else:
                receipt['credential_source'] = ('env:TYPESAFE_API_KEY' if os.environ.get('TYPESAFE_API_KEY', '').strip()
                                                else 'file:' + str(key_file) if key_file else None)
                if key:
                    credential_source = receipt['credential_source']
                    pair, receipt = choose(task, request.get('catalog'), key, jev_model, deadline,
                                           ordinary=ordinary, capability_provenance=capability,
                                           allowed_models=allowed_models, allowed_efforts=allowed_efforts,
                                           saved_routing_policy=saved_routing_policy,
                                           transport=transport, max_task_bytes=max_task_bytes)
                    receipt['credential_source'] = credential_source
    except ModelPolicyError:
        return {'status': 'refused', 'receipt': {'status': 'refused', 'reason': 'worker_model_policy'}}
    except Exception as error:
        receipt.update(status='fallback', reason='native_bridge_' + type(error).__name__)
    if isinstance(request, dict) and request.get('explicit_choice') is not None and pair is not None:
        choice = request['explicit_choice']
        receipt['human_override'] = attribution
        if (pair['model'], pair['effort']) != (choice['model'], choice['effort']):
            pair = None
            receipt.update(status='refused', reason='human_constraint_not_selected')
    selected = bool(pair and receipt['status'] == 'selected')
    confirmed = bool(pair and receipt.get('status') == 'confirmed_ordinary')
    receipt['assessment_status'] = ('selected' if selected else 'confirmed_ordinary' if confirmed
        else 'uncertain' if receipt.get('reason') == 'assessment_uncertain'
        else 'insufficient' if receipt.get('reason') == 'assessment_insufficient'
        else 'unavailable')
    if selected:
        receipt['status'] = 'choice_proposed'
    elif not confirmed:
        receipt['status'] = 'refused' if capability is not None else 'fallback'
    args = spawn_agent_request(pair, receipt) if selected or confirmed else None
    dispatch_status = 'selected' if selected else 'ordinary_confirmed'
    dispatch_receipt = compact_dispatch_receipt(args, dispatch_status, receipt)
    if dispatch_receipt is not None:
        receipt.update(spawn_agent_request=dispatch_receipt, spawn_arguments='ready_not_invoked')
    if capability is None:
        receipt['native_execution_selection'] = ('unconfirmed_pending_native_snapshot_preflight'
                                                 if selected else 'assessed_ordinary_confirmation' if confirmed else 'ordinary_fallback')
    receipt.update(preparation_stage='native_prepared_context_choice',
                   elapsed_ms=round((time.monotonic() - started) * 1000))
    status = ('selected' if selected else 'confirmed_ordinary' if confirmed and capability is not None
              else 'refused' if capability is not None else 'fallback')
    result = {'status': status, 'receipt': receipt}
    if selected or confirmed:
        result.update(model=pair['model'], effort=pair['effort'])
    if args is not None:
        result.update(spawn_agent=args, dispatch_receipt=dispatch_receipt)
    return result


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}', value)


def _receipt_path_text(value):
    """Preserve filename spelling; reject controls that break receipt/marker framing."""
    value = os.fspath(value)
    if (not isinstance(value, str) or not value
            or any(unicodedata.category(char) in {'Cc', 'Cs', 'Zl', 'Zp'} for char in value)):
        raise ValueError('receipt paths must not contain control or line separators')
    return value


def _task_workdir(root, work_dir):
    path = Path(_receipt_path_text(work_dir))
    path = path if path.is_absolute() else root / path
    _receipt_path_text(path)
    # Check the supplied spelling before resolve() hides even in-work redirects.
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('receipt directory path must not contain symlinks')
    if '..' in path.parts:
        raise ValueError('receipt directory traversal refused')
    path = path.resolve()
    work = root / 'work'
    if work.is_symlink() or not path.is_relative_to(work) or path == work:
        raise ValueError('an explicit project-local work/<task> receipt directory is required')
    # The resolved path must also remain inside this project task work tree.
    return path


def publish_routing_capabilities(root, capability):
    """Atomically publish a fresh native capability catalog for the bound project."""
    supplied_root = Path(root).absolute()
    if any(part.is_symlink() for part in (supplied_root, *supplied_root.parents)):
        raise ValueError('project root cannot contain symlinks')
    root = supplied_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('project root is not a directory')
    directory = root / '.factory'
    if directory.is_symlink() or not directory.is_dir() or not directory.resolve().is_relative_to(root):
        raise ValueError('project factory directory is missing or unsafe')
    captured = capability.get('captured_at') if isinstance(capability, dict) else None
    now = time.time()
    if (isinstance(captured, bool) or not isinstance(captured, (int, float))
            or not math.isfinite(captured) or not 0 <= now - captured <= 60):
        raise ValueError('capability metadata expired; refresh from current native tool schema')
    rows = capability.get('models')
    if (capability.get('source') != 'current_runtime_tool_schema'
            or capability.get('tool') != 'collaboration.spawn_agent'
            or not _identifier(capability.get('caller_id'))
            or not isinstance(rows, list) or not 1 <= len(rows) <= 32
            or any(not isinstance(row, dict) or not isinstance(row.get('model'), str)
                   or not isinstance(row.get('reasoning_efforts'), list)
                   or len(row['reasoning_efforts']) > 16
                   or any(not isinstance(effort, str) for effort in row['reasoning_efforts'])
                   for row in rows)):
        raise ValueError('malformed current native tool metadata')
    pairs = collaboration_capability_pairs(capability)
    models = {}
    for row in pairs.values():
        models.setdefault(row['model'], set()).add(row['effort'])
    if not models:
        raise ValueError('native capability catalog is empty')
    catalog = {'generated_at': datetime.fromtimestamp(captured, timezone.utc).isoformat(),
               'models': {model: sorted(efforts) for model, efforts in sorted(models.items())}}
    destination = directory / 'routing-capabilities.json'
    if destination.is_symlink() or not destination.resolve(strict=False).is_relative_to(root):
        raise ValueError('routing capability catalog path is unsafe')
    fd, temporary = tempfile.mkstemp(prefix='.routing-capabilities-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as stream:
            json.dump(catalog, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        sync_directory(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return catalog



def withhold_launch_arguments(result):
    """Keep assessment evidence, never callable argument copies, after write failure."""
    result.pop('model', None)
    result.pop('effort', None)

    def strip(value):
        if isinstance(value, dict):
            for name in ('spawn_agent', 'dispatch_receipt', 'spawn_agent_request',
                         'native_request', 'native_task_name', 'native_transport'):
                value.pop(name, None)
            for nested in value.values():
                strip(nested)
        elif isinstance(value, list):
            for nested in value:
                strip(nested)

    strip(result)
    result['receipt']['spawn_arguments'] = 'unavailable'

def prepare_subagent(request, root, caller_id, work_dir, *, transport=None):
    """Project-bound shared entry. All capability metadata is caller supplied."""
    from .consumer import read_settings, project_binding, shared_root, prepared_request
    root = shared_root(Path(root).resolve())
    settings = read_settings(root)
    binding = project_binding(root)
    if not isinstance(request, dict) or request.get('schema') != 'CPI411SUBAGENTV1':
        raise ValueError('subagent request schema refused')
    if not _identifier(caller_id) or request.get('caller_id') != caller_id:
        raise ValueError('caller identity mismatch')
    if request.get('project_binding') != binding:
        raise ValueError('subagent project binding mismatch')
    if not _identifier(request.get('task_id')):
        raise ValueError('task identity required')
    task = request.get('task')
    if not isinstance(task, str) or not task.strip() or len(task.encode('utf8')) > 8192:
        raise ValueError('a compact nonempty bounded task is required')
    capability = request.get('capability_provenance')
    if not isinstance(capability, dict) or capability.get('caller_id') != caller_id:
        raise ValueError('capability caller mismatch')
    captured = capability.get('captured_at')
    now = time.time()
    if (isinstance(captured, bool) or not isinstance(captured, (int, float))
            or not math.isfinite(captured) or not 0 <= now - captured <= 60):
        raise ValueError('capability metadata expired; refresh from current native tool schema')
    # Refuse invalid provenance even for an explicit choice, before any key access.
    collaboration_capability_pairs(capability)
    if 'catalog' in request:
        raise ValueError('shared subagent entry requires current native tool metadata')
    directory = _task_workdir(root, work_dir)
    policy = prepared_request(settings, capability)
    config = settings.get('jev_router', {})
    packet = request.get('native_dispatch')
    native = native_dispatch_binding(packet) if packet is not None else None
    routed = dict(request)
    if native is not None:
        routed['task'] = task + '\nNative dispatch task ' + packet['task_name'] + ':\n' + packet['message']
        if len(routed['task'].encode('utf8')) > 8192:
            raise ValueError('native dispatch task packet exceeds assessment bound')
    result = route(routed, root, config.get('key_file'), config.get('jev_model', 'jev-latest'),
                   allowed_models=policy['allowed_models'], allowed_efforts=policy['allowed_efforts'],
                   saved_routing_policy=policy['saved_routing_policy'], transport=transport)
    if native is not None:
        result['native_dispatch'] = native
    identity = 'jev-' + uuid.uuid4().hex
    path = directory / (identity + '.json')
    metadata = {'preparation_id': identity, 'caller_id': caller_id,
                'task_id': request['task_id'], 'project_binding': binding,
                'source_binding': {'revision': settings.get('revision'), 'source': settings.get('source')},
                'capability_identity_sha256': hashlib.sha256(json.dumps(capability, sort_keys=True).encode()).hexdigest(),
                'capability_captured_at': captured, 'capability_expires_at': captured + 60,
                'capability_provenance': 'caller_supplied_tool_metadata',
                'request_sha256': hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
                'work_dir': str(directory), 'receipt_path': str(path),
                'retention_owner': caller_id, 'cleanup_condition': 'retain through original-caller acceptance and rollback need',
                'dispatch': 'not_invoked', 'result': 'not_observed', 'acceptance': 'not_performed',
                'observed_model': None, 'observed_effort': None, 'worker_usage': None,
                'before_dispatch': 'refresh caller metadata and verify project/caller identity; reconcile uncertain sends, never blind redispatch'}
    project_policy = {'allowed_models': policy['allowed_models']}
    if policy['allowed_efforts'] is not None:
        project_policy['allowed_efforts'] = policy['allowed_efforts']
    if policy['saved_routing_policy'] is not None:
        project_policy['saved_routing_policy'] = policy['saved_routing_policy']
    metadata['project_model_policy'] = project_policy
    result['receipt']['project_model_policy'] = project_policy
    if native is not None and result.get('status') in SUCCESS and 'spawn_agent' in result:
        locator = path.relative_to(root).as_posix()
        metadata['native_transport'] = native_transport_binding(locator, native)
        metadata['native_task_name'] = metadata['native_transport']['task_name']
        result['native_request'] = {'task_name': metadata['native_task_name'],
                                    'message': packet['message'], **result['spawn_agent']}
    result.update(metadata)
    if 'dispatch_receipt' in result:
        result['dispatch_receipt'].update(metadata)
    result['receipt'].update(metadata)
    result['persistence'] = {'status': 'persisted', 'path': str(path)}
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Exclusive create prevents a collision from replacing another receipt.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf8') as stream:
                json.dump(result, stream, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(directory)
        except Exception:
            path.unlink(missing_ok=True)
            raise
    except OSError:
        # A chosen/overridden pair remains evidence, but unpersisted preparation
        # never exposes launch-ready arguments, including nested copies.
        result['receipt'].update(assessment_outcome_reason=result['receipt']['reason'],
            reason='native_bridge_receipt_unavailable', status='refused', spawn_arguments='unavailable')
        withhold_launch_arguments(result)
        result['status'] = 'refused'
        result['persistence'] = {'status': 'failed', 'path': str(path),
                                 'required_action': 'restore receipt persistence and prepare again before dispatch'}
    if result['persistence']['status'] == 'persisted':
        publish_routing_capabilities(root, capability)
    return result


SUCCESS = {'selected', 'confirmed_ordinary'}


def sync_directory(directory):
    if os.name != 'nt':
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def native_dispatch_binding(packet):
    if (not isinstance(packet, dict) or set(packet) != {'task_name', 'message'}
            or not isinstance(packet['task_name'], str)
            or not re.fullmatch(r'[a-z0-9_]+', packet['task_name'])
            or not isinstance(packet['message'], str) or not packet['message'].strip()
            or len(packet['message'].encode('utf8')) > 8192
            or '[CPI414 preparation=' in packet['message']):
        raise ValueError('exact native dispatch task name/message required')
    return {'task_name': packet['task_name'],
            'message_sha256': hashlib.sha256(packet['message'].encode('utf8')).hexdigest()}


# The platform exposes task_name as an unencrypted lowercase/digit/underscore
# string, with no declared length cap. Actual long-name acceptance is unproved.
NATIVE_CARRIER_PREFIX = 'cpi414_'


def native_carrier(locator):
    locator = _receipt_path_text(locator)
    path = Path(locator)
    if (path.is_absolute() or '..' in path.parts or path.as_posix() != locator
            or not re.fullmatch(r'work/.+/jev-[0-9a-f]{32}\.json', locator)):
        raise ValueError('native_carrier_locator_invalid')
    return NATIVE_CARRIER_PREFIX + locator.encode('utf8').hex()


def native_carrier_locator(task_name):
    if not isinstance(task_name, str) or not re.fullmatch(r'cpi414_[0-9a-f]+', task_name):
        raise ValueError('native_carrier_invalid')
    try:
        locator = bytes.fromhex(task_name[len(NATIVE_CARRIER_PREFIX):]).decode('utf8')
        if native_carrier(locator) != task_name:
            raise ValueError('noncanonical carrier')
    except (ValueError, UnicodeError):
        raise ValueError('native_carrier_invalid') from None
    return locator


def native_transport_binding(locator, declared):
    return {'schema': 'CPI414NATIVETRANSPORTV1', 'carrier': 'task_name_hex_locator',
            'receipt_locator': locator, 'task_name': native_carrier(locator),
            'declared_task': declared}


def private_json(root, locator):
    """Read only a bounded owned private regular receipt below this project's work."""
    if not isinstance(locator, str) or not locator:
        raise ValueError('receipt locator required')
    path = Path(_receipt_path_text(locator))
    path = path if path.is_absolute() else root / path
    _task_workdir(root, path.parent)
    if path.is_symlink() or path.resolve().parent != path.parent.resolve():
        raise ValueError('receipt symlink refused')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_nlink != 1
                or hasattr(os, 'getuid') and info.st_uid != os.getuid()
                or info.st_size > MAX_CONTEXT):
            raise ValueError('receipt must be a private owned bounded regular file')
        with os.fdopen(fd, 'r', encoding='utf8') as stream:
            fd = None
            result = json.load(stream)
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(result, dict):
        raise ValueError('receipt object required')
    return path.resolve(), result


def persist_private(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(path.parent)
    except Exception:
        # A write may have reached storage. Preserve uncertainty instead of deleting
        # an admission reservation and permitting a duplicate launch.
        raise


def provider_catalog(request):
    from .jev_router import eligible
    provenance = request.get('catalog_provenance')
    catalog = request.get('catalog')
    if (not isinstance(provenance, dict)
            or set(provenance) != {'source', 'connection_id', 'captured_at'}
            or provenance['source'] != 'codex_app_server_same_connection'
            or not isinstance(provenance['connection_id'], str)):
        raise ValueError('same-connection catalog provenance required')
    uuid.UUID(provenance['connection_id'])
    if (not isinstance(catalog, dict) or catalog.get('identity') != provenance['connection_id']
            or catalog.get('fetched_at') != provenance['captured_at']
            or isinstance(provenance['captured_at'], bool)
            or not isinstance(provenance['captured_at'], (int, float))):
        raise ValueError('catalog connection/freshness mismatch')
    pairs = eligible(catalog, time.time())
    return provenance, pairs


def checked_human_attribution(value):
    """Require actual caller verification metadata, without claiming authentication."""
    if (not isinstance(value, dict) or set(value) != {'checked', 'checked_by', 'checked_at'}
            or value.get('checked') is not True
            or not isinstance(value.get('checked_by'), str) or not value['checked_by'].strip()
            or len(value['checked_by']) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in value['checked_by'])
            or not isinstance(value.get('checked_at'), str) or len(value['checked_at']) > 128):
        raise ModelPolicyError('checked human attribution metadata required')
    try:
        timestamp = value['checked_at']
        parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if 'T' not in timestamp or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('aware timestamp required')
    except (ValueError, OverflowError):
        raise ModelPolicyError('human attribution ISO timestamp required') from None
    return dict(value)


def prepare_worker(request, root, caller_id, work_dir, *, reuse_receipt=None, transport=None):
    """Mandatory project-bound provider preparation; same-task reuse is read-only."""
    from .consumer import read_settings, project_binding, shared_root, verify_source
    root = shared_root(Path(root).resolve())
    settings = read_settings(root)
    verified_source = verify_source(settings)
    source_binding = {'revision': settings.get('revision'), 'source': str(verified_source)}
    binding = project_binding(root)
    if (not isinstance(request, dict) or request.get('schema') != 'CPI414PROVIDERWORKERV1'
            or request.get('caller_id') != caller_id or not _identifier(caller_id)
            or not _identifier(request.get('task_id')) or request.get('project_binding') != binding):
        raise ValueError('provider worker identity/binding refused')
    if ('capability_provenance' in request or not isinstance(request.get('task'), str)
            or not request['task'].strip() or len(request['task'].encode('utf8')) > 65536):
        raise ValueError('bounded provider task required')
    if 'continuation' in request and not isinstance(request['continuation'], bool):
        raise ValueError('continuation must be boolean')
    provenance, pairs = provider_catalog(request)
    directory = _task_workdir(root, work_dir)
    task_hash = hashlib.sha256(request['task'].encode('utf8')).hexdigest()
    verification = None
    if request.get('explicit_choice') is not None:
        verification = checked_human_attribution(request.get('human_attribution_verification'))
    if reuse_receipt is not None or request.get('continuation'):
        if not request.get('continuation') or reuse_receipt is None:
            raise ValueError('same-task continuation requires exact original receipt; reconcile before dispatch')
        path, previous = private_json(root, reuse_receipt)
        if (path.parent != directory or previous.get('receipt_path') != str(path)
                or previous.get('work_dir') != str(directory)
                or previous.get('preparation_id') != path.stem
                or not re.fullmatch(r'jev-[0-9a-f]{32}', path.stem)
                or previous.get('persistence', {}).get('path') != str(path)
                or previous.get('schema') != 'CPI414PROVIDERWORKERV1'
                or previous.get('caller_id') != caller_id or previous.get('task_id') != request['task_id']
                or previous.get('project_binding') != binding
                or previous.get('source_binding') != source_binding
                or previous.get('persistence', {}).get('status') != 'persisted'
                or previous.get('status') not in SUCCESS):
            raise ValueError('continuation receipt identity/persistence mismatch')
        assert_actual_assessment(previous.get('receipt'), previous.get('model'), previous.get('effort'))
        original_attribution = previous.get('receipt', {}).get('human_override')
        if verification is not None and (
                verification != previous.get('human_attribution_verification')
                or human_override(request['explicit_choice']) != original_attribution
                or (request['explicit_choice']['model'], request['explicit_choice']['effort']) !=
                   (previous.get('model'), previous.get('effort'))):
            raise ModelPolicyError('continuation cannot replace original human constraint')
        # A continued turn may have a different prompt. Logical task identity and
        # original assessment are durable; do not re-assess physical transport forks.
        model, effort = previous.get('model'), previous.get('effort')
        if not any((r['model'], r['effort']) == (model, effort) for r in pairs.values()):
            raise ModelPolicyError('continued pair unavailable; reconcile without substitution')
        return {**previous, 'reuse': 'same_logical_task', 'current_catalog_provenance': provenance}
    assert_ordinary(request.get('ordinary'))
    if not any((row['model'], row['effort']) ==
               (request['ordinary']['model'], request['ordinary']['effort']) for row in pairs.values()):
        raise ModelPolicyError('ordinary pair unavailable in authenticated catalog')
    config = settings.get('jev_router', {})
    if not isinstance(config, dict):
        raise ValueError('invalid project router configuration')
    result = route(request, root, config.get('key_file'), config.get('jev_model', 'jev-latest'),
                   transport=transport, max_task_bytes=65536)
    if verification is not None:
        result['human_attribution_verification'] = verification
        result['receipt']['human_attribution_verification'] = verification
    if result['receipt'].get('assessment_status') == 'confirmed_ordinary':
        result['status'] = 'confirmed_ordinary'
    if result.get('status') not in SUCCESS:
        result['status'] = 'refused'
        result.pop('model', None)
        result.pop('effort', None)
    identity = 'jev-' + uuid.uuid4().hex
    path = directory / (identity + '.json')
    result.update(schema='CPI414PROVIDERWORKERV1', preparation_id=identity,
                  caller_id=caller_id, task_id=request['task_id'], project_binding=binding,
                  source_binding=source_binding,
                  task_sha256=task_hash, catalog_provenance=provenance,
                  work_dir=str(directory), receipt_path=str(path), dispatch='not_invoked',
                  observed_model=None, observed_effort=None, worker_usage=None,
                  retention_owner=caller_id,
                  cleanup_condition='retain through original-caller acceptance and rollback need',
                  persistence={'status': 'persisted', 'path': str(path)})
    try:
        persist_private(path, result)
    except OSError:
        result['receipt'].update(assessment_outcome_reason=result['receipt'].get('reason'),
                                 reason='native_bridge_receipt_unavailable', status='refused')
        withhold_launch_arguments(result)
        result.update(status='refused', persistence={
            'status': 'failed', 'path': str(path),
            'required_action': 'restore receipt persistence and prepare again before dispatch'})
    return result


def read_request(stream):
    raw = stream.read(MAX_CONTEXT + 1)
    if len(raw) > MAX_CONTEXT:
        raise ValueError('context bound')
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--credential-file')
    parser.add_argument('--jev-model', default='jev-latest')
    args = parser.parse_args()
    try:
        from .consumer import read_settings
        # The legacy public module also enforces the existing project binding.
        read_settings(Path(args.root).resolve())
        result = route(read_request(sys.stdin.buffer), args.root, args.credential_file, args.jev_model)
        output = json.dumps(result, separators=(',', ':')).encode()
        if len(output) > 65536:
            raise ValueError('response bound')
    except Exception:
        output = b'{"status":"refused","receipt":{"reason":"native_bridge_protocol"}}'
    sys.stdout.buffer.write(output)


if __name__ == '__main__':
    main()
