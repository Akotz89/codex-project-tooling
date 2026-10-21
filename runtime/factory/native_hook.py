"""Synchronous native spawn policy. Host callback failures can still fail open."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
import time
import uuid
from pathlib import Path
from .jev_prepared_bridge import (read_request, private_json, persist_private,
                                  native_dispatch_binding, native_transport_binding, native_carrier_locator,
                                  NATIVE_CARRIER_PREFIX, _identifier, _receipt_path_text, SUCCESS)
from .jev_router import allowed_pair, assert_actual_assessment

MARKER = re.compile(r'\n\[CPI414 preparation=(work/.+/jev-[0-9a-f]{32}\.json)\]\Z')
TOOLS = {'spawn_agent', 'collaboration.spawn_agent', 'collaborationspawn_agent',
         'functions.spawn_agent', 'multi_agent.spawn_agent', 'Agent'}
ALLOWED_ARGS = {'task_name', 'message', 'model', 'reasoning_effort', 'fork_turns'}


REFUSAL_REASONS = ['native_actual_assessment_refused', 'native_arguments_invalid', 'native_caller_mismatch', 'native_caller_provenance_invalid', 'native_capability_expired', 'native_carrier_invalid', 'native_carrier_locator_invalid', 'native_event_identity_invalid', 'native_event_kind_invalid', 'native_packet_pair_mismatch', 'native_plaintext_marker_required', 'native_post_pair_mismatch', 'native_post_reservation_mismatch', 'native_preparation_binding_mismatch', 'native_prepared_pair_refused', 'native_project_policy_invalid', 'native_project_policy_binding_mismatch', 'native_project_root_mismatch', 'native_session_uuid_invalid', 'native_tool_identity_invalid', 'native_transport_binding_mismatch', 'native_transport_mode_mismatch']


def refusal_reason(error):
    if isinstance(error, ValueError) and str(error) in REFUSAL_REASONS:
        return str(error)
    if isinstance(error, FileExistsError):
        return 'native_dispatch_already_reserved'
    if isinstance(error, FileNotFoundError):
        return 'native_receipt_missing'
    if isinstance(error, OSError):
        return 'native_private_io_refused'
    return 'native_validation_refused'


def deny(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny', 'permissionDecisionReason': reason}}


def event_identity(event):
    result = {key: event.get(key) for key in ('session_id', 'turn_id', 'tool_use_id')}
    if not all(_identifier(value) for value in result.values()):
        raise ValueError('native_event_identity_invalid')
    return result


def dispatch_input(event):
    if event.get('tool_name') not in TOOLS:
        raise ValueError('native_tool_identity_invalid')
    arguments = event.get('tool_input')
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if (not isinstance(arguments, dict) or set(arguments) - ALLOWED_ARGS
            or not isinstance(arguments.get('message'), str) or not arguments['message'].strip()):
        raise ValueError('native_arguments_invalid')
    if isinstance(arguments.get('task_name'), str) and arguments['task_name'].startswith(NATIVE_CARRIER_PREFIX):
        return arguments, native_carrier_locator(arguments['task_name']), None
    match = MARKER.search(arguments['message'])
    if match is None:
        raise ValueError('native_plaintext_marker_required')
    locator = _receipt_path_text(match.group(1))
    body = arguments['message'][:match.start()]
    binding = native_dispatch_binding({'task_name': arguments.get('task_name'), 'message': body})
    return arguments, locator, binding


def native_result_handle(response):
    """Recognize supported spawn results, without widening generic identifiers."""
    if (not isinstance(response, dict)
            or set(response) - {'agent_id', 'agentId', 'task_name', 'nickname'}):
        return None
    handles = []
    for key in ('agent_id', 'agentId', 'task_name'):
        if key not in response or (key != 'task_name' and response[key] is None):
            continue
        value = response[key]
        if key == 'task_name':
            # V2: /root followed by the lowercase/digit/underscore task segments.
            valid = isinstance(value, str) and re.fullmatch(r'/root(?:/[a-z0-9_]+)+', value)
        else:
            valid = _identifier(value)
        if not valid:
            return None
        handles.append(value)
    # Distinct aliases cannot be joined to one child from this result alone.
    return handles[0] if handles and len(set(handles)) == 1 else None


def native_caller_mapping(prepared, session_id):
    """WSL ThreadId only: raw UUID or exact native:<same UUID>, no task-path inference."""
    try:
        if not isinstance(session_id, str) or str(uuid.UUID(session_id)) != session_id:
            raise ValueError('native_session_uuid_invalid')
    except (ValueError, AttributeError):
        raise ValueError('native_session_uuid_invalid') from None
    if (prepared.get('capability_provenance') != 'caller_supplied_tool_metadata'
            or prepared.get('receipt', {}).get('capability_tool') != 'collaboration.spawn_agent'):
        raise ValueError('native_caller_provenance_invalid')
    caller = prepared.get('caller_id')
    if caller == session_id:
        return 'exact_session_uuid'
    if caller == 'native:' + session_id:
        return 'native_prefix_session_uuid'
    raise ValueError('native_caller_mismatch')

def validate(event, root, *, fresh=True):
    from .consumer import shared_root, read_settings, project_binding, verify_source
    from .jev_router import validate_allowed_models
    root = shared_root(Path(root).resolve())
    if (not isinstance(event, dict) or not isinstance(event.get('cwd'), str)
            or shared_root(Path(event['cwd']).resolve()) != root):
        raise ValueError('native_project_root_mismatch')
    identity = event_identity(event)
    settings = read_settings(root)
    verify_source(settings)
    config = settings.get('jev_router', {})
    if not isinstance(config, dict):
        raise ValueError('native_project_policy_invalid')
    current_models = validate_allowed_models(config['allowed_models']) if 'allowed_models' in config else None
    current_policy = {'allowed_models': current_models}
    arguments, locator, native = dispatch_input(event)
    path, prepared = private_json(root, locator)
    if native is not None and any(key in prepared for key in ('native_transport', 'native_task_name', 'native_request')):
        raise ValueError('native_transport_mode_mismatch')
    expected_id = path.stem
    if (prepared.get('preparation_id') != expected_id or prepared.get('receipt_path') != str(path)
            or prepared.get('work_dir') != str(path.parent)
            or prepared.get('project_binding') != project_binding(root)
            or prepared.get('source_binding') != {'revision': settings.get('revision'), 'source': settings.get('source')}
            or not native_caller_mapping(prepared, identity['session_id'])
            or not _identifier(prepared.get('task_id'))
            or (native is not None and prepared.get('native_dispatch') != native)
            or prepared.get('persistence', {}).get('status') != 'persisted'
            or prepared.get('persistence', {}).get('path') != str(path)
            or prepared.get('status') not in SUCCESS):
        raise ValueError('native_preparation_binding_mismatch')
    captured = prepared.get('capability_captured_at')
    expires = prepared.get('capability_expires_at')
    if fresh and (isinstance(captured, bool) or not isinstance(captured, (float, int))
            or not 0 <= time.time() - captured <= 60 or expires != captured + 60):
        raise ValueError('native_capability_expired')
    receipt = prepared.get('receipt', {})
    pair = prepared.get('spawn_agent', {})
    if ('project_model_policy' in prepared or 'allowed_models' in config):
        if prepared.get('project_model_policy') != current_policy or receipt.get('project_model_policy') != current_policy:
            raise ValueError('native_project_policy_binding_mismatch')
    if (receipt.get('capability_provenance') != 'caller_supplied_tool_metadata'
            or receipt.get('capability_tool') != 'collaboration.spawn_agent'
            or not isinstance(pair, dict) or set(pair) != {'model', 'reasoning_effort', 'fork_turns'}
            or pair.get('fork_turns') != 'none'
            or not allowed_pair(pair.get('model'), pair.get('reasoning_effort'))
            or current_models is not None and pair.get('model') not in current_models
            or not any(isinstance(row, dict) and
                       (row.get('model'), row.get('effort')) ==
                       (pair['model'], pair['reasoning_effort'])
                       for row in receipt.get('spawn_capability_pairs', []))):
        raise ValueError('native_prepared_pair_refused')
    try:
        assert_actual_assessment(receipt, pair['model'], pair['reasoning_effort'])
    except (ValueError, TypeError, AttributeError):
        raise ValueError('native_actual_assessment_refused') from None
    if native is None:
        declared = prepared.get('native_dispatch')
        packet = prepared.get('native_request')
        if (not isinstance(declared, dict) or set(declared) != {'task_name', 'message_sha256'}
                or prepared.get('native_transport') != native_transport_binding(locator, declared)
                or prepared.get('native_task_name') != arguments['task_name']
                or not isinstance(packet, dict) or set(packet) != ALLOWED_ARGS
                or packet.get('task_name') != arguments['task_name']
                or native_dispatch_binding({'task_name': declared['task_name'],
                                            'message': packet.get('message')}) != declared
                or any(packet.get(key) != value for key, value in pair.items())):
            raise ValueError('native_transport_binding_mismatch')
        if any(arguments.get(key) != value for key, value in pair.items()):
            raise ValueError('native_packet_pair_mismatch')
    return root, identity, arguments, path, prepared, pair


def handle(event, root):
    """No raw task/output/error text is retained or emitted on failure."""
    name = event.get('hook_event_name') if isinstance(event, dict) else None
    try:
        if name not in {'PreToolUse', 'PostToolUse'}:
            raise ValueError('native_event_kind_invalid')
        root, identity, args, path, prepared, pair = validate(event, root, fresh=name == 'PreToolUse')
        consumption = path.with_suffix('.consume.json')
        if name == 'PreToolUse':
            rewritten = {**args, **pair}
            transport = prepared.get('native_transport')
            record = {'schema': 'CPI414NATIVECONSUMPTIONV2',
                      'preparation_id': prepared['preparation_id'],
                      'task_id': prepared['task_id'], 'project_binding': prepared['project_binding'],
                      'identity': identity, 'status': 'pending_native_result',
                      'prepared_caller_id': prepared['caller_id'],
                      'caller_mapping': native_caller_mapping(prepared, identity['session_id']),
                      'native_transport': transport,
                      'declared_task': prepared['native_dispatch'],
                      'declared_message_sha256': prepared['native_dispatch']['message_sha256'],
                      'observed_message_sha256': hashlib.sha256(args['message'].encode('utf8')).hexdigest(),
                      'observed_pre_arguments_sha256': hashlib.sha256(
                          json.dumps(args, sort_keys=True).encode()).hexdigest(),
                      'plaintext_attestation': 'unavailable' if transport else 'exact_legacy_marker_body',
                      'task_semantics_acceptance': 'original_caller_useful_result_required',
                      'prepared_pair': pair, 'requested_pair': {key: args.get(key) for key in pair},
                      'applied_arguments_sha256': hashlib.sha256(
                          json.dumps(rewritten, sort_keys=True).encode()).hexdigest(),
                      'observed_model': None, 'observed_effort': None, 'worker_usage': None,
                      'before_retry': 'reconcile actual native liveness; never blind redispatch'}
            # An exclusive reservation precedes rewrite. Existing pending/uncertain
            # consumption refuses even if the same event is delivered again.
            persist_private(consumption, record)
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                    'permissionDecision': 'allow', 'updatedInput': rewritten}}
        _, reserved = private_json(root, str(consumption))
        # Post-tool input is evidence of what was supplied to the tool. Never
        # normalize a conflicting actual pair into the expected pair before
        # verification: that would turn the preparation into false consumption.
        if any(args.get(key) != value for key, value in pair.items()):
            raise ValueError('native_post_pair_mismatch')
        actual_arguments_sha256 = hashlib.sha256(
            json.dumps(args, sort_keys=True).encode()).hexdigest()
        if (reserved.get('task_id') != prepared['task_id']
                or reserved.get('project_binding') != prepared['project_binding']
                or reserved.get('prepared_caller_id') != prepared['caller_id']
                or reserved.get('native_transport') != prepared.get('native_transport')
                or reserved.get('declared_task') != prepared['native_dispatch']
                or reserved.get('identity') != identity
                or reserved.get('preparation_id') != prepared['preparation_id']
                or reserved.get('prepared_pair') != pair
                or reserved.get('applied_arguments_sha256') != actual_arguments_sha256):
            raise ValueError('native_post_reservation_mismatch')
        response = event.get('tool_response')
        if isinstance(response, str):
            response = json.loads(response)
        handle_id = native_result_handle(response)
        observed = handle_id is not None
        persist_private(path.with_suffix('.result.json'),
                        {'schema': 'CPI414NATIVERESULTV2', 'identity': identity,
                         'preparation_id': prepared['preparation_id'],
                         'status': 'native_handle_observed' if observed else 'unknown_native_result',
                         'native_handle': handle_id if observed else None,
                         'declared_task': prepared['native_dispatch'],
                         'observed_post_message_sha256': hashlib.sha256(args['message'].encode('utf8')).hexdigest(),
                         'observed_post_arguments_sha256': actual_arguments_sha256,
                         'plaintext_attestation': reserved['plaintext_attestation'],
                         'task_semantics_acceptance': 'original_caller_useful_result_required',
                         'observed_model': None, 'observed_effort': None,
                         'worker_usage': None, 'acceptance': 'not_performed'})
        return {}
    except Exception as error:
        if name == 'PostToolUse':
            return {'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                    'additionalContext': 'Native worker consumption requires reconciliation: ' + refusal_reason(error)}}
        return deny('Native worker preparation refused: ' + refusal_reason(error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    try:
        event = read_request(sys.stdin.buffer)
        result = handle(event, args.root)
    except Exception as error:
        result = deny('Native worker policy input refused: ' + refusal_reason(error))
    print(json.dumps(result, sort_keys=True))
    return 0
