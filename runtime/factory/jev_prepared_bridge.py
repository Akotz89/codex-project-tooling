"""Prepare native requests only; the original caller owns dispatch and acceptance."""
from pathlib import Path
import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import uuid
from .jev_router import (choose, http_choice, read_credential, ModelPolicyError,
                        SPAWN_AGENT_EFFORTS, collaboration_capability_pairs, assert_ordinary)

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


def human_override(choice):
    """Validate caller attribution; the coordinator checks the actual human source."""
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
        if set(reference) != {'kind', 'path'} or not isinstance(path, str) or not Path(path).is_absolute() or not Path(path).is_file():
            raise ModelPolicyError('resolvable governing instruction reference required')
    else:
        raise ModelPolicyError('human override reference kind refused')
    return {'source': choice['source'], 'instruction_quote': quote, 'instruction_reference': dict(reference)}


def explicit_spawn_request(choice, capability_provenance, *, ordinary):
    attribution = human_override(choice)
    pairs = collaboration_capability_pairs(capability_provenance)
    # Override bypasses assessment, never validation of the supplied ordinary context.
    assert_ordinary(ordinary)
    if not any(row['model'] == ordinary['model'] and row['effort'] == ordinary['effort']
               for row in pairs.values()):
        raise ModelPolicyError('ordinary worker pair is unsupported by current tool capability')
    if not any(row['model'] == choice['model'] and row['effort'] == choice['effort']
               for row in pairs.values()):
        raise ModelPolicyError('explicit spawn choice is unsupported by current tool capability')
    receipt = {'schema': 'CPI236SPAWNREQUESTV1', 'status': 'human_override',
               'reason': 'direct_human_instruction', 'attempts': 0, 'assessment_status': 'unassessed_human_override',
               'human_override': attribution, 'ordinary_worker': dict(ordinary),
               'prepared_pair': {'model': choice['model'], 'effort': choice['effort']},
               'capability_provenance': 'caller_supplied_tool_metadata',
               'capability_tool': capability_provenance['tool'],
               'spawn_capability_pairs': [{'model': row['model'], 'effort': row['effort']}
                                          for _, row in sorted(pairs.items())]}
    args = spawn_agent_request(choice, receipt)
    if args is None:
        raise ModelPolicyError('explicit spawn choice failed model policy')
    return {'status': 'human_override', 'spawn_agent': args,
            'dispatch_receipt': compact_dispatch_receipt(args, 'human_override', receipt),
            'receipt': receipt}


def route(request, root, key_file=None, jev_model='jev-latest', *, transport=None):
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
        if request.get('explicit_choice') is not None:
            if capability is None:
                raise ModelPolicyError('explicit spawn choice requires current tool metadata')
            return explicit_spawn_request(request['explicit_choice'], capability, ordinary=request.get('ordinary'))
        remaining, task, ordinary = request['remaining_ms'], request['task'], request['ordinary']
        if (isinstance(remaining, bool) or not isinstance(remaining, (int, float))
                or not math.isfinite(remaining) or not 0 < remaining <= 8000
                or not isinstance(task, str)):
            raise ValueError('native context')
        if capability is not None and 'catalog' in request:
            raise ValueError('choose one capability provenance source')
        deadline = started + remaining / 1000
        # Validate ordinary policy/capability before credential IO; ordinary is context only.
        _, receipt = choose(task, request.get('catalog'), None, jev_model, deadline,
                            ordinary=ordinary, capability_provenance=capability, transport=transport)
        if 'ordinary_worker' in receipt:
            try:
                key = read_credential(key_file)
            except Exception:
                receipt['reason'] = 'credential_unavailable'
            else:
                if key:
                    pair, receipt = choose(task, request.get('catalog'), key, jev_model, deadline,
                                           ordinary=ordinary, capability_provenance=capability,
                                           transport=transport)
    except ModelPolicyError:
        return {'status': 'refused', 'receipt': {'status': 'refused', 'reason': 'worker_model_policy'}}
    except Exception as error:
        receipt.update(status='fallback', reason='native_bridge_' + type(error).__name__)
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
    if selected:
        result.update(model=pair['model'], effort=pair['effort'])
    if args is not None:
        result.update(spawn_agent=args, dispatch_receipt=dispatch_receipt)
    return result


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}', value)


def _task_workdir(root, work_dir):
    path = Path(work_dir)
    path = path if path.is_absolute() else root / path
    # Check the supplied spelling before resolve() hides even in-work redirects.
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('receipt directory path must not contain symlinks')
    path = path.resolve()
    work = root / 'work'
    if work.is_symlink() or not path.is_relative_to(work) or path == work:
        raise ValueError('an explicit project-local work/<task> receipt directory is required')
    # The resolved path must also remain inside this project task work tree.
    return path


def prepare_subagent(request, root, caller_id, work_dir, *, transport=None):
    """Project-bound shared entry. All capability metadata is caller supplied."""
    from .consumer import read_settings, project_binding, shared_root
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
    config = settings.get('jev_router', {})
    if not isinstance(config, dict):
        raise ValueError('invalid project router configuration')
    result = route(request, root, config.get('key_file'), config.get('jev_model', 'jev-latest'),
                   transport=transport)
    identity = 'jev-' + uuid.uuid4().hex
    path = directory / (identity + '.json')
    metadata = {'preparation_id': identity, 'caller_id': caller_id,
                'task_id': request['task_id'], 'project_binding': binding,
                'capability_identity_sha256': hashlib.sha256(json.dumps(capability, sort_keys=True).encode()).hexdigest(),
                'capability_captured_at': captured, 'capability_expires_at': captured + 60,
                'capability_provenance': 'caller_supplied_tool_metadata',
                'request_sha256': hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
                'work_dir': str(directory), 'receipt_path': str(path),
                'retention_owner': caller_id, 'cleanup_condition': 'retain through original-caller acceptance and rollback need',
                'dispatch': 'not_invoked', 'result': 'not_observed', 'acceptance': 'not_performed',
                'observed_model': None, 'observed_effort': None, 'worker_usage': None,
                'before_dispatch': 'refresh caller metadata and verify project/caller identity; reconcile uncertain sends, never blind redispatch'}
    result.update(metadata)
    if 'dispatch_receipt' in result:
        result['dispatch_receipt'].update(metadata)
    result['receipt'].update(metadata)
    result['persistence'] = {'status': 'persisted', 'path': str(path)}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Exclusive create prevents a collision from replacing another receipt.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf8') as stream:
                json.dump(result, stream, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
    except OSError:
        # A chosen/overridden pair remains evidence, but unpersisted preparation
        # never exposes launch-ready arguments, including nested copies.
        result['receipt'].update(assessment_outcome_reason=result['receipt']['reason'],
            reason='native_bridge_receipt_unavailable', status='refused', spawn_arguments='unavailable')
        for key in ('model', 'effort', 'spawn_agent', 'dispatch_receipt'):
            result.pop(key, None)
        result['receipt'].pop('spawn_agent_request', None)
        result['status'] = 'refused'
        result['persistence'] = {'status': 'failed', 'path': str(path),
                                 'required_action': 'restore receipt persistence and prepare again before dispatch'}
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
