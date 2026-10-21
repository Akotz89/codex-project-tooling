"""Bounded typed Jev selection intersected with current capabilities and owner policy."""
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
MAX_BYTES = 131072
class ModelPolicyError(ValueError):
    pass

def allowed_pair(model, effort):
    return isinstance(model, str) and isinstance(effort, str) and effort in WORKER_EFFORTS.get(model, set())

def assert_ordinary(ordinary):
    if not isinstance(ordinary, dict) or set(ordinary) != {'provider', 'model', 'effort'} or ordinary.get('provider') != 'codex' or not allowed_pair(ordinary.get('model'), ordinary.get('effort')):
        raise ModelPolicyError('CPI236 worker policy refused ordinary/explicit selection; use supported Luna6 efforts through max or Sol6.1 low/medium/high; no silent replacement')

def validate_allowed_models(models):
    if (not isinstance(models, list) or not models
            or any(not isinstance(model, str) or model not in WORKER_EFFORTS for model in models)
            or len(models) != len(set(models))):
        raise ModelPolicyError('project allowed_models must be a nonempty unique list within shared worker policy')
    return list(models)

WORKER_EFFORTS = {
    'gpt-6-luna': {'low', 'medium', 'high', 'xhigh', 'max'},
    'gpt-6.1-sol': {'low', 'medium', 'high'},
}
# Native collaboration currently supports this same policy intersection.
SPAWN_AGENT_EFFORTS = WORKER_EFFORTS


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

# Versioned accepted priors are bound by the existing immutable runtime digest.
ECONOMIC_CONTEXT = {'version': 'CPI404_COST_CONTEXT_20261021_V1',
 'basis': 'Accepted retained public evidence; no runtime research or measurement.',
 'comparison_rule': 'Task-grounded qualitative cross-model choice is allowed. Preserve model identity and '
                    'protocol/date/harness confounds; quantitative comparisons require compatible panels. '
                    'No invalid pooled metric, combined rank or cross-family Pareto claim. Benchmark '
                    'quality is not local acceptance probability. Null is unknown, never zero.',
 'model_mapping': 'Exact advertised app slug and public model ID; immutable provider checkpoint unverified.',
 'panels': [{'benchmark': 'VulcanBench Frontier v4 retired-binary parity',
             'model': 'gpt-6-luna',
             'protocol': '3.16',
             'solver_cli': '0.155.0',
             'report_date': '2026-09-28',
             'accessed': '2026-10-20',
             'source_url': 'https://vulcanbench.com/assets/data/swe-v4-gpt6-luna-v316/runs.csv',
             'evidence_class': 'publisher measurement; raw arithmetic reproduced; not independent '
                               'replication',
             'columns': ['effort',
                         'quality_score_100_timeouts_zero',
                         'standard_error',
                         'task_passes',
                         'attempts',
                         'timeouts',
                         'api_equivalent_mean_usd_priced_attempts',
                         'priced_n',
                         'cost_complete',
                         'end_to_end_mean_minutes'],
             'rows': [['low', 40.831, 2.0415, 0, 23, 0, 0.02779, 23, True, 5.5576],
                      ['medium', 45.7192, 1.3221, 0, 23, 0, 0.009535, 23, True, 3.5593],
                      ['high', 57.3514, 3.7954, 2, 23, 0, 0.051912, 23, True, 15.5904],
                      ['xhigh', 71.9354, 5.4728, 11, 23, 2, 0.159686, 21, False, 52.5745],
                      ['max', 67.2629, 6.9935, 12, 23, 4, 0.097529, 19, False, 60.5342]],
             'ttft_seconds': None,
             'generation_tokens_per_second': None},
            {'benchmark': 'DeepSWE v1.1 Score',
             'model': 'gpt-6-luna',
             'protocol': 'DeepSWE v1.1 label; evaluation run/harness details unknown',
             'source_url': 'https://openai.com/index/introducing-gpt-6-sol-and-luna/',
             'figure_identifier': 'chart-wrapper-6yH2LVSbmONCIcmQ9ntEQU',
             'report_date': None,
             'accessed': '2026-10-21T01:02:44.613197+00:00',
             'evidence_class': 'first-party benchmark chart observation',
             'columns': ['effort', 'quality_percent', 'published_cost_per_task_usd'],
             'rows': [['low', 2.4, 0.0057],
                      ['medium', 44.5, 0.052],
                      ['high', 59.3, 0.084],
                      ['xhigh', 61.3, 0.11],
                      ['max', 66.6, 0.22]],
             'timing_seconds': None,
             'sample_count': None,
             'confidence_interval': None},
            {'benchmark': 'VulcanBench Frontier v4 retired-binary parity',
             'model': 'gpt-6.1-sol',
             'protocol': '3.18',
             'solver_cli': '0.159.0',
             'report_date': '2026-10-01',
             'accessed': '2026-10-20',
             'source_url': 'https://vulcanbench.com/assets/data/swe-v4-gpt61-sol-v318/runs.csv',
             'evidence_class': 'publisher measurement; raw arithmetic reproduced; not independent '
                               'replication',
             'columns': ['effort',
                         'quality_score_100_timeouts_zero',
                         'standard_error',
                         'task_passes',
                         'attempts',
                         'timeouts',
                         'api_equivalent_mean_usd_priced_attempts',
                         'priced_n',
                         'cost_complete',
                         'end_to_end_mean_minutes'],
             'rows': [['low', 86.2213, 0.8218, 20, 23, 0, 0.305976, 23, True, 6.89],
                      ['medium', 86.441, 0.7823, 22, 23, 0, 0.3986, 23, True, 10.5647],
                      ['high', 88.2342, 0.4008, 23, 23, 0, 0.33007, 23, True, 10.2219]],
             'ttft_seconds': None,
             'generation_tokens_per_second': None},
            {'benchmark': 'DeepSWE v1.1 Score',
             'model': 'gpt-6.1-sol',
             'protocol': 'DeepSWE v1.1 label; evaluation run/harness details unknown',
             'source_url': 'https://openai.com/index/introducing-gpt-6-1-sol/',
             'figure_identifier': 'chart-wrapper-deepswe',
             'report_date': None,
             'accessed': '2026-10-21T01:02:44.613197+00:00',
             'evidence_class': 'first-party benchmark chart observation',
             'columns': ['effort', 'quality_percent', 'published_cost_per_task_usd'],
             'rows': [['low', 64.4, 0.17], ['medium', 73.0, 0.42], ['high', 75.2, 0.65]],
             'timing_seconds': None,
             'sample_count': None,
             'confidence_interval': None}],
 'api_prices': [{'model': 'gpt-6-luna',
                 'accessed': '2026-10-20',
                 'source_url': 'https://developers.openai.com/api/docs/models/gpt-6-luna',
                 'service_tier': 'Standard <=272K input, no regional premium; tool/cache-write charges '
                                 'excluded',
                 'units': 'USD per million tokens',
                 'input': 0.1,
                 'cached_input': 0.01,
                 'output': 0.5},
                {'model': 'gpt-6.1-sol',
                 'accessed': '2026-10-20',
                 'source_url': 'https://developers.openai.com/api/docs/models/gpt-6.1-sol',
                 'service_tier': 'Standard <=272K input, no regional premium; tool/cache-write charges '
                                 'excluded',
                 'units': 'USD per million tokens',
                 'input': 2,
                 'cached_input': 0.1,
                 'output': 10}],
 'local_accounting': {'provider_billing_usd': None,
                      'subscription_quota': None,
                      'accepted_task_total_cost_usd': None,
                      'acceptance_probability': None,
                      'elapsed_seconds': None,
                      'rework_cost_usd': None},
 'limitations': ['Public API prices, publisher benchmark priced attempts, local token counters, provider '
                 'billing and subscription quota are distinct accounting bases.',
                 'Luna timeout costs are missing; priced-attempt means exclude unpriced attempts. TTFT and '
                 'generation speed are unmeasured.',
                 'DeepSWE chart points are publisher reports, not independent replication; separate '
                 'publication charts do not establish identical evaluation runs or harnesses.',
                 'No comparative local accepted-outcome measurement or savings claim.']}


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
            if not allowed_pair(model, effort):
                continue
            ident = 'pair_' + hashlib.sha256(f'{model}\0{effort}'.encode()).hexdigest()[:16]
            pairs[ident] = {'model': model, 'effort': effort, 'description': str(row.get('description', ''))[:256], 'effort_description': effort_description}
    if not (0 if allow_empty else 1) <= len(pairs) <= 63:
        raise ValueError('catalog size')
    return pairs


def capability_description(value, credential=None):
    """Optional tool metadata: ignore malformed text, redact before its 256-char cap."""
    if not isinstance(value, str) or not value.strip():
        return ''
    return redact_task(value, credential)[:256]


def collaboration_capability_pairs(provenance, *, credential=None):
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
        description = capability_description(row.get('description'), credential)
        effort_descriptions = row.get('reasoning_effort_descriptions')
        if not isinstance(effort_descriptions, dict):
            effort_descriptions = {}
        for effort in efforts:
            if not isinstance(effort, str) or effort not in SPAWN_AGENT_EFFORTS[model]:
                continue
            ident = 'pair_' + hashlib.sha256(f'{model}\0{effort}'.encode()).hexdigest()[:16]
            pairs[ident] = {'model': model, 'effort': effort, 'description': description,
                            'effort_description': capability_description(effort_descriptions.get(effort), credential)}
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
        http_status = response.status
        request_id = response.headers.get('x-request-id') or response.headers.get('request-id')
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
    answer = json.loads(b''.join(chunks))
    if isinstance(answer, dict):
        answer['_http'] = {'status': http_status, 'request_id': request_id}
    return answer


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


def selection_questions(pairs, credential=None):
    """Build one closed-set Choice from the actual allowed capability intersection."""
    questions = copy.deepcopy(QUESTIONS)
    criteria = questions['worker_pair']['criteria']
    for key, row in sorted(pairs.items()):
        row = {name: value.replace(credential, '[REDACTED]') if credential and isinstance(value, str) else value
               for name, value in row.items()}
        criteria[key] = (f"Select model {row['model']} at reasoning effort {row['effort']} "
                         f"for this task. Model capability: {row['description']}. "
                         f"Effort capability: {row['effort_description']}.")
    return questions


def assessments(answer, questions):
    """Validate the complete typed distribution, without a suitability threshold."""
    if not isinstance(answer, dict) or not isinstance(answer.get('answers'), dict) or set(answer['answers']) != set(questions):
        raise ValueError('assessment answer set')
    result = {}
    for name, question in questions.items():
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


def selected_pair(items, pairs):
    item = items['worker_pair']
    selected = item['choice']
    if selected == 'insufficient_information':
        return None, 'assessment_insufficient'
    if selected == 'uncertain' or sum(math.isclose(p, item['probabilities'][selected], abs_tol=1e-12, rel_tol=0)
                                      for p in item['probabilities'].values()) != 1:
        return None, 'assessment_uncertain'
    return pairs[selected], 'direct_pair_selected'


def assert_actual_assessment(receipt, model, effort):
    """Reject legacy/manual receipts at consumption, including same-task reuse."""
    if (not isinstance(receipt, dict) or receipt.get('schema') != 'CPI236TASKASSESSMENTV1'
            or type(receipt.get('attempts')) is not int or receipt['attempts'] != 1
            or receipt.get('assessment_status') not in {'selected', 'confirmed_ordinary'}
            or not re.fullmatch(r'[0-9a-f]{64}', str(receipt.get('request_sha256', '')))
            or receipt.get('prepared_pair') != {'model': model, 'effort': effort}):
        raise ModelPolicyError('actual persisted Jev assessment required')
    rows = receipt.get('assessment_capability_pairs', receipt.get('spawn_capability_pairs', receipt.get('catalog_pairs')))
    if not isinstance(rows, list) or not rows:
        raise ModelPolicyError('assessed capability set missing')
    pairs = {}
    for row in rows:
        if not isinstance(row, dict) or not allowed_pair(row.get('model'), row.get('effort')):
            raise ModelPolicyError('assessed capability pair refused')
        ident = 'pair_' + hashlib.sha256(f"{row['model']}\0{row['effort']}".encode()).hexdigest()[:16]
        pairs[ident] = {**row, 'description': '', 'effort_description': ''}
    items = assessments({'answers': {k: {'type': 'choice', **v} for k, v in receipt.get('assessments', {}).items()}},
                        selection_questions(pairs))
    pair, _ = selected_pair(items, pairs)
    if pair is None or (pair['model'], pair['effort']) != (model, effort):
        raise ModelPolicyError('actual Jev selection mismatch')


def choose(task, catalog, credential, jev_model, deadline, *, ordinary=None,
           capability_provenance=None, allowed_models=None, transport=http_choice, max_task_bytes=MAX_TASK_BYTES):
    assert_ordinary(ordinary)  # Cannot fall through missing-key or provider-error paths.
    started = time.monotonic()
    receipt = {'schema': 'CPI236TASKASSESSMENTV1', 'policy': 'direct_permitted_pair_choice_1',
               'status': 'fallback', 'reason': 'unavailable', 'attempts': 0,
               'context_source': 'natural_work', 'certainty_policy': 'unique_choice_no_calibrated_suitability_threshold'}
    try:
        observed_pairs = (collaboration_capability_pairs(capability_provenance, credential=credential)
                          if capability_provenance is not None else eligible(catalog, time.time(), allow_empty=True))
        permitted_models = validate_allowed_models(allowed_models) if allowed_models is not None else None
        pairs = ({key: row for key, row in observed_pairs.items()
                  if row['model'] in permitted_models} if permitted_models is not None else observed_pairs)
        ordinary = dict(ordinary)
        if not any(row['model'] == ordinary['model'] and row['effort'] == ordinary['effort'] for row in observed_pairs.values()):
            raise ModelPolicyError('CPI236 ordinary worker pair is unavailable in the supported options; no fallback launch')
        pair_field = 'spawn_capability_pairs' if capability_provenance is not None else 'catalog_pairs'
        receipt.update(**{pair_field: [{'model': row['model'], 'effort': row['effort']} for _, row in sorted(observed_pairs.items())]},
                       ordinary_worker=ordinary)
        if permitted_models is not None:
            receipt.update(assessment_capability_pairs=[{'model': row['model'], 'effort': row['effort']}
                                                        for _, row in sorted(pairs.items())],
                           project_allowed_models=permitted_models)
            if not pairs:
                raise ModelPolicyError('project policy leaves no currently supported worker pair')
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
        if receipt['task_utf8_bytes'] > max_task_bytes:
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
                   'state': {'task': task, 'ordinary_worker': ordinary, 'allowed_catalog': allowed_catalog,
                             'economic_context': copy.deepcopy(ECONOMIC_CONTEXT)},
                   'questions': selection_questions(pairs, credential)}
        body = json.dumps(payload, separators=(',', ':')).encode()
        if len(body) > MAX_BYTES:
            receipt['reason'] = 'request_limit'
            return None, receipt
        receipt['request_sha256'] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        receipt.update(endpoint='https://api.typesafe.ai/v1/systemone', decision_model=jev_model)
        receipt['attempts'] = 1
        answer = transport(payload, credential, deadline)
        meta = answer.get('_transport') if isinstance(answer, dict) else None
        if isinstance(meta, dict) and isinstance(meta.get('child_pid'), int) and isinstance(meta.get('reaped'), bool):
            receipt['transport'] = {'child_pid': meta['child_pid'], 'reaped': meta['reaped']}
        http = answer.get('_http') if isinstance(answer, dict) else None
        if isinstance(http, dict):
            if type(http.get('status')) is int and 100 <= http['status'] <= 599:
                receipt['http_status'] = http['status']
            ident = http.get('request_id')
            if isinstance(ident, str) and re.fullmatch(r'[A-Za-z0-9._:/-]{1,128}', ident) and credential not in ident:
                receipt['returned_request_id'] = ident
        for field in ('request_id', 'status'):
            value = answer.get(field) if isinstance(answer, dict) else None
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:/-]{1,128}', value) and credential not in value:
                receipt['returned_' + field] = value
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
            items = assessments(answer, payload['questions'])
        except ValueError:
            receipt['reason'] = 'assessment_malformed'
            return None, receipt
        receipt['assessments'] = items
        pair, reason = selected_pair(items, pairs)
        receipt.update(assessment_reason=reason, execution_policy='unchanged')
        if pair is None:
            receipt['reason'] = reason
            return None, receipt
        receipt['prepared_pair'] = {'model': pair['model'], 'effort': pair['effort']}
        if (pair['model'], pair['effort']) == (ordinary['model'], ordinary['effort']):
            receipt.update(status='confirmed_ordinary', reason='ordinary_equivalent',
                           native_execution_selection='assessed_ordinary_confirmation')
            return pair, receipt
        receipt.update(status='selected', reason='direct_pair_selected')
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
