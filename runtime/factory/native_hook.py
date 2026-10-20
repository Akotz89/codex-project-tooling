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
                                  native_dispatch_binding, _identifier, _receipt_path_text, SUCCESS)
from .jev_router import allowed_pair, assert_actual_assessment

MARKER = re.compile(r'\n\[CPI414 preparation=(work/.+/jev-[0-9a-f]{32}\.json)\]\Z')
TOOLS = {'spawn_agent', 'collaboration.spawn_agent', 'functions.spawn_agent',
         'multi_agent.spawn_agent', 'Agent'}
ALLOWED_ARGS = {'task_name', 'message', 'model', 'reasoning_effort', 'fork_turns'}


def deny(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny', 'permissionDecisionReason': reason}}


def event_identity(event):
    result = {key: event.get(key) for key in ('session_id', 'turn_id', 'tool_use_id')}
    if not all(_identifier(value) for value in result.values()):
        raise ValueError('native session/turn/tool-use identity required')
    return result


def dispatch_input(event):
    if event.get('tool_name') not in TOOLS:
        raise ValueError('unsupported native spawn tool identity')
    arguments = event.get('tool_input')
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if (not isinstance(arguments, dict) or set(arguments) - ALLOWED_ARGS
            or not isinstance(arguments.get('message'), str)):
        raise ValueError('native spawn arguments refused')
    match = MARKER.search(arguments['message'])
    if match is None:
        raise ValueError('task-bound native preparation marker required')
    locator = _receipt_path_text(match.group(1))
    body = arguments['message'][:match.start()]
    binding = native_dispatch_binding({'task_name': arguments.get('task_name'), 'message': body})
    return arguments, locator, binding



def native_caller_mapping(prepared, session_id):
    """WSL ThreadId only: raw UUID or exact native:<same UUID>, no task-path inference."""
    try:
        if not isinstance(session_id, str) or str(uuid.UUID(session_id)) != session_id:
            raise ValueError('canonical native session UUID required')
    except (ValueError, AttributeError):
        raise ValueError('canonical native session UUID required') from None
    if (prepared.get('capability_provenance') != 'caller_supplied_tool_metadata'
            or prepared.get('receipt', {}).get('capability_tool') != 'collaboration.spawn_agent'):
        raise ValueError('native caller mapping requires collaboration-tool preparation')
    caller = prepared.get('caller_id')
    if caller == session_id:
        return 'exact_session_uuid'
    if caller == 'native:' + session_id:
        return 'native_prefix_session_uuid'
    raise ValueError('foreign or unsupported native caller identity')

def validate(event, root, *, fresh=True):
    from .consumer import shared_root, read_settings, project_binding, verify_source
    root = shared_root(Path(root).resolve())
    if (not isinstance(event, dict) or not isinstance(event.get('cwd'), str)
            or shared_root(Path(event['cwd']).resolve()) != root):
        raise ValueError('native hook project root mismatch')
    identity = event_identity(event)
    settings = read_settings(root)
    verify_source(settings)
    arguments, locator, native = dispatch_input(event)
    path, prepared = private_json(root, locator)
    expected_id = path.stem
    if (prepared.get('preparation_id') != expected_id or prepared.get('receipt_path') != str(path)
            or prepared.get('work_dir') != str(path.parent)
            or prepared.get('project_binding') != project_binding(root)
            or prepared.get('source_binding') != {'revision': settings.get('revision'), 'source': settings.get('source')}
            or not native_caller_mapping(prepared, identity['session_id'])
            or not _identifier(prepared.get('task_id'))
            or prepared.get('native_dispatch') != native
            or prepared.get('persistence', {}).get('status') != 'persisted'
            or prepared.get('persistence', {}).get('path') != str(path)
            or prepared.get('status') not in SUCCESS):
        raise ValueError('native preparation task/caller/root/persistence mismatch')
    captured = prepared.get('capability_captured_at')
    expires = prepared.get('capability_expires_at')
    if fresh and (isinstance(captured, bool) or not isinstance(captured, (float, int))
            or not 0 <= time.time() - captured <= 60 or expires != captured + 60):
        raise ValueError('native preparation capability expired')
    receipt = prepared.get('receipt', {})
    pair = prepared.get('spawn_agent', {})
    if (receipt.get('capability_provenance') != 'caller_supplied_tool_metadata'
            or receipt.get('capability_tool') != 'collaboration.spawn_agent'
            or not isinstance(pair, dict) or set(pair) != {'model', 'reasoning_effort', 'fork_turns'}
            or pair.get('fork_turns') != 'none'
            or not allowed_pair(pair.get('model'), pair.get('reasoning_effort'))
            or not any(isinstance(row, dict) and
                       (row.get('model'), row.get('effort')) ==
                       (pair['model'], pair['reasoning_effort'])
                       for row in receipt.get('spawn_capability_pairs', []))):
        raise ValueError('native prepared pair capability/policy refused')
    assert_actual_assessment(receipt, pair['model'], pair['reasoning_effort'])
    return root, identity, arguments, path, prepared, pair


def handle(event, root):
    """No raw task/output/error text is retained or emitted on failure."""
    name = event.get('hook_event_name') if isinstance(event, dict) else None
    try:
        if name not in {'PreToolUse', 'PostToolUse'}:
            raise ValueError('native hook event refused')
        root, identity, args, path, prepared, pair = validate(event, root, fresh=name == 'PreToolUse')
        consumption = path.with_suffix('.consume.json')
        if name == 'PreToolUse':
            rewritten = {**args, **pair}
            record = {'schema': 'CPI414NATIVECONSUMPTIONV1',
                      'preparation_id': prepared['preparation_id'],
                      'task_id': prepared['task_id'], 'project_binding': prepared['project_binding'],
                      'identity': identity, 'status': 'pending_native_result',
                      'prepared_caller_id': prepared['caller_id'],
                      'caller_mapping': native_caller_mapping(prepared, identity['session_id']),
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
            raise ValueError('post-tool applied pair mismatch')
        actual_arguments_sha256 = hashlib.sha256(
            json.dumps(args, sort_keys=True).encode()).hexdigest()
        if (reserved.get('identity') != identity
                or reserved.get('preparation_id') != prepared['preparation_id']
                or reserved.get('prepared_pair') != pair
                or reserved.get('applied_arguments_sha256') != actual_arguments_sha256):
            raise ValueError('post-tool dispatch identity mismatch')
        response = event.get('tool_response')
        if isinstance(response, str):
            response = json.loads(response)
        handle_id = response.get('agent_id') if isinstance(response, dict) else None
        if handle_id is None and isinstance(response, dict):
            handle_id = response.get('agentId')
        observed = _identifier(handle_id)
        persist_private(path.with_suffix('.result.json'),
                        {'schema': 'CPI414NATIVERESULTV1', 'identity': identity,
                         'preparation_id': prepared['preparation_id'],
                         'status': 'native_handle_observed' if observed else 'unknown_native_result',
                         'native_handle': handle_id if observed else None,
                         'observed_model': None, 'observed_effort': None,
                         'worker_usage': None, 'acceptance': 'not_performed'})
        return {}
    except Exception as error:
        if name == 'PostToolUse':
            return {'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                    'additionalContext': 'Native worker consumption requires reconciliation: ' + type(error).__name__}}
        return deny('Native worker preparation refused (' + type(error).__name__ +
                    '); verify exact task/caller/project, fresh private receipt, and reconcile prior dispatch.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    try:
        event = read_request(sys.stdin.buffer)
        result = handle(event, args.root)
    except Exception as error:
        result = deny('Native worker policy input refused (' + type(error).__name__ + ').')
    print(json.dumps(result, sort_keys=True))
    return 0
