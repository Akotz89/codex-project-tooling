"""CPI236LIMITEDV1: one opt-in Choice before a fresh native implement launch."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import subprocess
import sys
import time
from pathlib import Path
import urllib.request

FLAG = '--jev-route'
GENERATED_CONFIG_FLAG = '--jev-generated-config'
MAX_BYTES = 65536
THRESHOLD = 0.8  # Initial conservative acceptance rule; not a calibrated optimal threshold.
class ModelPolicyError(ValueError):
    pass

def allowed_pair(model, effort):
    return isinstance(model, str) and isinstance(effort, str) and ((model == 'gpt-6-luna' and effort in NATIVE_EFFORTS) or (model == 'gpt-6.1-sol' and effort in {'low', 'medium', 'high'}))

def assert_ordinary(ordinary):
    if not isinstance(ordinary, dict) or set(ordinary) != {'provider', 'model', 'effort'} or ordinary.get('provider') != 'codex' or not allowed_pair(ordinary.get('model'), ordinary.get('effort')):
        raise ModelPolicyError('CPI236 worker policy refused ordinary/explicit selection; use Luna6 or Sol6.1 at low/medium/high; no silent replacement')

NATIVE_EFFORTS = {'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'persistent'}
SPAWN_AGENT_EFFORTS = {
    'gpt-6-luna': {'low', 'medium', 'high', 'xhigh', 'max'},
    'gpt-6.1-sol': {'low', 'medium', 'high'},
}


def probability(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1 and math.isfinite(value)


def options(args):
    return args[:args.index('--')] if '--' in args else args


def strip_opt_in(args):
    before = options(args)
    requested = FLAG in before
    generated_config = None
    explicit_config = False
    cleaned = []
    index = 0
    while index < len(before):
        arg = before[index]
        if arg == FLAG:
            index += 1
            continue
        if arg == '--config' or arg.startswith('--config='):
            explicit_config = True
        if arg == GENERATED_CONFIG_FLAG or arg.startswith(GENERATED_CONFIG_FLAG + '='):
            if not requested:
                raise ValueError(f'{GENERATED_CONFIG_FLAG} requires {FLAG}')
            if generated_config is not None:
                raise ValueError(f'{GENERATED_CONFIG_FLAG} may be supplied once')
            if '=' in arg:
                generated_config = arg.split('=', 1)[1]
                index += 1
            else:
                if index + 1 >= len(before) or before[index + 1].startswith('--'):
                    raise ValueError(f'{GENERATED_CONFIG_FLAG} requires a path')
                generated_config = before[index + 1]
                index += 2
            if not generated_config:
                raise ValueError(f'{GENERATED_CONFIG_FLAG} requires a path')
            cleaned.extend(['--config', generated_config])
            continue
        cleaned.append(arg)
        index += 1
    if generated_config is not None and explicit_config:
        raise ValueError('Use either --config or --jev-generated-config, not both')
    return cleaned + args[len(before):], requested, generated_config


def resolve_generated_config(args, generated_config, root):
    """Resolve generated config relative to the factory project and keep argv in sync."""
    path = Path(generated_config)
    if not path.is_absolute():
        path = Path(root) / path
    resolved = str(path.resolve())
    before = options(args)
    for index, arg in enumerate(before):
        if arg == '--config' and index + 1 < len(before) and before[index + 1] == generated_config:
            args[index + 1] = resolved
            return args, resolved
    raise ValueError('Generated config path was lost during factory argument parsing')


def explicit_choice(args, generated_config=None):
    opts = options(args)[1:]
    if generated_config is not None:
        filtered = []
        index = 0
        while index < len(opts):
            arg = opts[index]
            if arg == '--config' and index + 1 < len(opts) and opts[index + 1] == generated_config:
                index += 2
                continue
            if arg == '--config=' + generated_config:
                index += 1
                continue
            filtered.append(arg)
            index += 1
        opts = filtered
    return any(arg.split('=', 1)[0] in {'--model', '--config', '--effort', '--reasoning-effort', '--thinking', '-c'}
               or re.match(r'^(?:model_)?reasoning_effort=|^effort=', arg)
               for arg in opts)


MAX_TASK_BYTES = 8192
QUESTIONS = json.loads((SHARED_SOURCE / 'template/factory/assessment_questions.json').read_text(encoding='utf8'))


def redact_task(task, credential):
    if not isinstance(task, str) or not task.strip():
        raise ValueError('task unavailable')
    text = task.replace(credential, '[REDACTED]') if credential else task
    text = re.sub(r'(?i)\b(?:api[_ -]?key|password|secret|authorization)\s*[:=]\s*\S+', '[REDACTED]', text)
    return re.sub(r'\b(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+)', '[REDACTED]', text)


def eligible(catalog, now, *, allow_empty=False):
    # The caller must provide live same-account discovery, not a display cache.
    if not isinstance(catalog, dict) or catalog.get('same_account') is not True or not isinstance(catalog.get('identity'), str) or len(catalog['identity']) > 4096:
        raise ValueError('catalog unavailable')
    age = now - catalog['fetched_at']
    if not math.isfinite(age) or not 0 <= age <= 60:
        raise ValueError('catalog stale')
    if not isinstance(catalog.get('models'), list) or not 1 <= len(catalog['models']) <= 32:
        raise ValueError('catalog size')
    pairs = {}
    for row in catalog['models']:
        model = row.get('model')
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', model):
            raise ValueError('unsupported model')
        efforts = row.get('supportedReasoningEfforts', [])
        if not isinstance(efforts, list) or len(efforts) > 16:
            raise ValueError('effort catalog size')
        for effort in efforts:
            effort_description = effort.get('description', '') if isinstance(effort, dict) else ''
            effort_description = effort_description[:256] if isinstance(effort_description, str) else ''
            effort = effort.get('reasoningEffort') if isinstance(effort, dict) else effort
            if effort not in NATIVE_EFFORTS or not allowed_pair(model, effort):
                continue
            ident = 'pair_' + hashlib.sha256(f'{model}\0{effort}'.encode()).hexdigest()[:16]
            pairs[ident] = {'model': model, 'effort': effort, 'description': str(row.get('description', ''))[:256], 'effort_description': effort_description}
    if not (0 if allow_empty else 1) <= len(pairs) <= 63:
        raise ValueError('catalog size')
    return pairs


def collaboration_capability_pairs(provenance):
    """Validate model options supplied from this runtime's spawn_agent tool schema."""
    # This marker describes the caller-supplied input format/source. It is not
    # cryptographic freshness, account-authentication, or catalog-success proof.
    if (not isinstance(provenance, dict)
            or provenance.get('source') != 'current_runtime_tool_schema'
            or provenance.get('tool') != 'collaboration.spawn_agent'):
        raise ValueError('collaboration capability provenance unavailable')
    models = provenance.get('models')
    if not isinstance(models, list) or not 1 <= len(models) <= 32:
        raise ValueError('collaboration capability size')
    pairs = {}
    for row in models:
        if not isinstance(row, dict):
            raise ValueError('collaboration model capability')
        model = row.get('model')
        efforts = row.get('reasoning_efforts')
        if not isinstance(model, str) or model not in SPAWN_AGENT_EFFORTS or not isinstance(efforts, list) or len(efforts) > 16:
            continue
        for effort in efforts:
            if not isinstance(effort, str) or effort not in SPAWN_AGENT_EFFORTS[model]:
                continue
            ident = 'pair_' + hashlib.sha256(f'{model}\0{effort}'.encode()).hexdigest()[:16]
            pairs[ident] = {'model': model, 'effort': effort, 'description': '', 'effort_description': ''}
    if not pairs:
        raise ValueError('collaboration capability options unavailable')
    return pairs


def _http_once(payload, credential, deadline):
    """One bounded stdlib HTTP request. No transport retries or worker launches."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('routing deadline')
    body = json.dumps(payload, separators=(',', ':')).encode()
    if len(body) > MAX_BYTES:
        raise ValueError('request limit')
    body = body.replace(credential.encode(), b'[REDACTED]')
    request = urllib.request.Request('https://api.typesafe.ai/v1/systemone', data=body,
        headers={'Authorization': 'Bearer ' + credential, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=remaining) as response:
        chunks = []
        size = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('routing deadline')
            part = response.read1(min(4096, MAX_BYTES + 1 - size))
            if not part:
                break
            chunks.append(part)
            size += len(part)
            if size > MAX_BYTES:
                raise ValueError('response limit')
    if time.monotonic() >= deadline:
        raise TimeoutError('routing deadline')
    return json.loads(b''.join(chunks))


class TransportFailure(RuntimeError):
    def __init__(self, kind, pid, reaped):
        super().__init__(kind)
        self.kind = kind
        self.transport = {'child_pid': pid, 'reaped': reaped}


def http_choice(payload, credential, deadline):
    """One finite HTTP child: credential on stdin only; hard overall deadline/reaping."""
    child = None
    try:
        data = json.dumps({'payload': payload, 'credential': credential, 'deadline': deadline}, separators=(',', ':')).encode()
        if len(data) > MAX_BYTES + 16384 or time.monotonic() >= deadline:
            raise TransportFailure('deadline_or_request_limit', None, True)
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--http-once'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={key: value for key, value in os.environ.items() if key != 'TYPESAFE_API_KEY'})
        output, _ = child.communicate(data, timeout=max(.001, deadline - time.monotonic()))
        if len(output) > MAX_BYTES or child.returncode or time.monotonic() >= deadline:
            raise TransportFailure('response_or_deadline', child.pid, True)
        response = json.loads(output)
        if response.get('ok') is not True or not isinstance(response.get('result'), dict):
            raise TransportFailure('http_or_protocol', child.pid, True)
        result = response['result']
        result['_transport'] = {'child_pid': child.pid, 'reaped': True}
        return result
    except subprocess.TimeoutExpired:
        raise TransportFailure('deadline', child.pid if child else None, False) from None
    except TransportFailure:
        raise
    except Exception:
        raise TransportFailure('spawn_or_protocol', child.pid if child else None, child is None or child.poll() is not None) from None
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()  # Exactly one child, always reaped; never retry the HTTP request.
        error = sys.exc_info()[1]
        if isinstance(error, TransportFailure) and child is not None:
            error.transport['reaped'] = child.poll() is not None


def _http_child():
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 16385)
        if len(raw) > MAX_BYTES + 16384:
            raise ValueError('request limit')
        data = json.loads(raw)
        result = _http_once(data['payload'], data['credential'], data['deadline'])
        output = json.dumps({'ok': True, 'result': result}, separators=(',', ':'), ensure_ascii=False).encode()
        if len(output) > MAX_BYTES:
            raise ValueError('response limit')
    except Exception as error:
        if hasattr(error, 'close'):
            error.close()
        output = b'{"ok":false}'
    sys.stdout.buffer.write(output)


def assessments(answer):
    """Validate every wire answer; certainty is checked only where mapping uses it."""
    if not isinstance(answer, dict) or not isinstance(answer.get('answers'), dict) or set(answer['answers']) != set(QUESTIONS):
        raise ValueError('assessment answer set')
    result = {}
    for name, question in QUESTIONS.items():
        item = answer['answers'][name]
        if not isinstance(item, dict):
            raise ValueError('assessment answer')
        ps = item.get('probabilities')
        selected = item.get('choice')
        if (item.get('type') != 'choice' or not isinstance(selected, str) or selected not in question['criteria']
                or not isinstance(ps, dict) or set(ps) != set(question['criteria'])
                or not all(probability(p) for p in ps.values()) or abs(sum(ps.values()) - 1) > .02
                or not probability(item.get('confidence')) or ps[selected] != max(ps.values())):
            raise ValueError('assessment wire')
        result[name] = {'choice': selected, 'confidence': item['confidence'], 'probabilities': dict(ps)}
    return result


def certain(item):
    return item['confidence'] >= THRESHOLD and item['probabilities'][item['choice']] >= THRESHOLD


def profile(items):
    used = ['task_kind', 'reasoning_demand']
    if any(items[name]['choice'] == 'insufficient_information' for name in used):
        return None, 'assessment_insufficient', used
    if not all(certain(items[name]) for name in used):
        return None, 'assessment_uncertain', used
    kind = items['task_kind']['choice']
    demand = items['reasoning_demand']['choice']
    if demand == 'difficult_coupled':
        return ('gpt-6.1-sol', 'high'), 'difficult_coupled', used
    if demand == 'diagnostic_or_cross_interface' or kind == 'diagnosis_or_design':
        return ('gpt-6.1-sol', 'medium'), 'diagnosis_or_cross_interface', used
    verification = items['verification_difficulty']
    if (demand == 'mechanical' and kind in {'mechanical', 'verification'}
            and verification['choice'] == 'direct_deterministic' and certain(verification)):
        return ('gpt-6-luna', 'low'), 'direct_mechanical', [*used, 'verification_difficulty']
    return ('gpt-6-luna', 'medium'), 'bounded_work', used


def choose(task, catalog, credential, jev_model, deadline, *, ordinary=None,
           capability_provenance=None, transport=http_choice):
    assert_ordinary(ordinary)  # Cannot fall through missing-key or provider-error paths.
    started = time.monotonic()
    receipt = {'schema': 'CPI236TASKASSESSMENTV1', 'policy': 'initial_task_matrix_1',
               'status': 'fallback', 'reason': 'unavailable', 'attempts': 0,
               'context_source': 'natural_work', 'certainty_policy': 'initial_uncalibrated_0.8'}
    try:
        pairs = (collaboration_capability_pairs(capability_provenance)
                 if capability_provenance is not None else eligible(catalog, time.time(), allow_empty=True))
        ordinary = dict(ordinary)
        if not any(row['model'] == ordinary['model'] and row['effort'] == ordinary['effort'] for row in pairs.values()):
            raise ModelPolicyError('CPI236 ordinary worker pair is unavailable in the supported options; no fallback launch')
        pair_field = 'spawn_capability_pairs' if capability_provenance is not None else 'catalog_pairs'
        receipt.update(**{pair_field: [{'model': row['model'], 'effort': row['effort']} for _, row in sorted(pairs.items())]},
                       ordinary_worker=ordinary)
        if capability_provenance is None:
            receipt.update(catalog_identity=hashlib.sha256(catalog['identity'].encode()).hexdigest(),
                           catalog_fetched_at=catalog['fetched_at'])
        else:
            receipt.update(capability_provenance='caller_supplied_tool_metadata',
                           capability_tool=capability_provenance['tool'])
        if not credential or not isinstance(credential, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', jev_model):
            return None, receipt
        if any(credential in value for value in ordinary.values()):
            raise ModelPolicyError('ordinary context invalid')
        if not isinstance(task, str) or not task.strip():
            receipt['reason'] = 'context_empty'
            return None, receipt
        task = redact_task(task, credential)
        try:
            receipt['task_utf8_bytes'] = len(task.encode('utf8'))
        except UnicodeError:
            receipt['reason'] = 'context_invalid'
            return None, receipt
        if receipt['task_utf8_bytes'] > MAX_TASK_BYTES:
            receipt['reason'] = 'context_oversize'
            return None, receipt
        if started >= deadline:
            receipt['reason'] = 'deadline'
            return None, receipt
        allowed_catalog = [{'model': row['model'], 'effort': row['effort'],
                            'description': row['description'].replace(credential, '[REDACTED]'),
                            'effort_description': row['effort_description'].replace(credential, '[REDACTED]')}
                           for _, row in sorted(pairs.items())]
        payload = {'model': jev_model,
                   'state': {'task': task, 'ordinary_worker': ordinary, 'allowed_catalog': allowed_catalog},
                   'questions': copy.deepcopy(QUESTIONS)}
        body = json.dumps(payload, separators=(',', ':')).encode()
        if len(body) > MAX_BYTES:
            receipt['reason'] = 'request_limit'
            return None, receipt
        receipt['request_sha256'] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        receipt['attempts'] = 1
        answer = transport(payload, credential, deadline)
        meta = answer.get('_transport') if isinstance(answer, dict) else None
        if isinstance(meta, dict) and isinstance(meta.get('child_pid'), int) and isinstance(meta.get('reaped'), bool):
            receipt['transport'] = {'child_pid': meta['child_pid'], 'reaped': meta['reaped']}
        # Usage is observable even when a late/malformed answer cannot select a worker.
        receipt['decision_model'] = jev_model
        returned_model = answer.get('model') if isinstance(answer, dict) else None
        receipt['returned_model'] = returned_model if isinstance(returned_model, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,128}', returned_model) and credential not in returned_model else None
        usage = answer.get('usage') if isinstance(answer, dict) else None
        receipt['usage'] = {key: value for key, value in usage.items()
                            if key in {'input_tokens', 'output_tokens', 'total_tokens'}
                            and isinstance(value, int) and not isinstance(value, bool) and value >= 0} if isinstance(usage, dict) else {}
        if time.monotonic() >= deadline:
            raise TimeoutError('routing deadline')
        try:
            items = assessments(answer)
        except ValueError:
            receipt['reason'] = 'assessment_malformed'
            return None, receipt
        receipt['assessments'] = items
        receipt.update(verification_context=items['verification_difficulty']['choice'],
            safeguard_context=items['material_consequence']['choice'], execution_policy='unchanged')
        mapped, reason, used = profile(items)
        receipt.update(assessment_reason=reason, certainty_features_used=used)
        if mapped is None:
            receipt['reason'] = reason
            return None, receipt
        receipt['mapped_pair'] = {'model': mapped[0], 'effort': mapped[1]}
        pair = next((row for row in pairs.values() if (row['model'], row['effort']) == mapped), None)
        if pair is None:
            receipt['reason'] = 'mapped_pair_unavailable'
            return None, receipt
        if mapped == (ordinary['model'], ordinary['effort']):
            receipt.update(reason='ordinary_equivalent', native_execution_selection='ordinary_no_change')
            return None, receipt
        receipt.update(status='selected', reason='profile_selected', prepared_pair=receipt['mapped_pair'])
        return pair, receipt
    except ModelPolicyError:
        raise
    except Exception as error:
        # Raw provider/error text can contain credentials or task content.
        receipt.update(status='fallback', reason='routing_' + type(error).__name__)
        if isinstance(error, TransportFailure):
            receipt.update(reason='transport_' + error.kind, transport=error.transport)
        if hasattr(error, 'close'):
            error.close()
        return None, receipt
    finally:
        receipt['elapsed_ms'] = round((time.monotonic() - started) * 1000)


def read_credential(key_file=None):
    value = os.environ.get('TYPESAFE_API_KEY', '').strip()
    if value:
        return value
    if not key_file:
        return None
    path = Path(key_file)
    stat = path.stat()
    if stat.st_mode & 0o077 or (hasattr(os, 'getuid') and stat.st_uid != os.getuid()):
        raise ValueError('credential file is not private')
    with path.open(encoding='utf8') as stream:
        value = stream.read(8193).strip()
    if len(value) > 8192:
        raise ValueError('credential size')
    return value or None


def configured_services(root, source, route):
    # Parent-owned adapter must rediscover from provider-resolved binary/account.
    # A catalog receipt supplied as JSON alone is never routing authority here.
    result = {'credential': lambda: read_credential(route.get('key_file')), 'jev_model': route.get('jev_model', 'jev-latest')}
    try:
        from jev_route import services
        result.update(services(root, source, route))
    except Exception:
        pass
    return result


def prepare(root, args, **_services):
    # An argv-only fallback cannot validate config/resume/default model resolution.
    raise ModelPolicyError('CPI236: legacy argv-only preflight is unsupported; use the pinned native factory consumer with final model-policy enforcement')


if __name__ == '__main__':
    if sys.argv[1:] != ['--http-once']:
        raise SystemExit(2)
    _http_child()
