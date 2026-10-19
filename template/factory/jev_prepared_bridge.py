"""Jev bridge: assessment-only native preflight or caller-supplied collaboration options; never spawns."""
from pathlib import Path
import argparse,json,math,sys,tempfile,time
from jev_router import choose,http_choice,read_credential,ModelPolicyError,SPAWN_AGENT_EFFORTS,collaboration_capability_pairs
MAX_CONTEXT=1024*1024


def spawn_agent_request(pair,receipt):
    """Return arguments only when runtime tool metadata supports this exact pair."""
    if not isinstance(pair,dict):return None
    model,effort=pair.get('model'),pair.get('effort')
    if model not in SPAWN_AGENT_EFFORTS or effort not in SPAWN_AGENT_EFFORTS[model]:return None
    if (receipt.get('capability_provenance') != 'caller_supplied_tool_metadata'
            or receipt.get('capability_tool') != 'collaboration.spawn_agent'):return None
    capabilities=receipt.get('spawn_capability_pairs')
    if not isinstance(capabilities,list) or not any(
            isinstance(row,dict) and row.get('model')==model and row.get('effort')==effort
            for row in capabilities):return None
    return {'model':model,'reasoning_effort':effort,'fork_turns':'none'}


def compact_dispatch_receipt(args,status,receipt):
    if args is None:return None
    return {'schema':'CPI236SPAWNREQUESTV1','status':status,
        'tool':'collaboration.spawn_agent','arguments':args,
        'dispatch':'not_invoked',
        'reason':receipt.get('reason'),'assessment_reason':receipt.get('assessment_reason'),
        'certainty_features_used':receipt.get('certainty_features_used'),
        'capability_provenance':receipt.get('capability_provenance'),
        'capability_tool':receipt.get('capability_tool'),
        'usage':receipt.get('usage',{})}


def explicit_spawn_request(choice,capability_provenance):
    pairs=collaboration_capability_pairs(capability_provenance)
    if not isinstance(choice,dict) or set(choice)!={'model','effort'}:
        raise ModelPolicyError('explicit spawn choice schema refused')
    if not any(row['model']==choice['model'] and row['effort']==choice['effort'] for row in pairs.values()):
        raise ModelPolicyError('explicit spawn choice is unsupported by current tool capability')
    receipt={'schema':'CPI236SPAWNREQUESTV1','status':'explicit','reason':'explicit_choice',
             'capability_provenance':'caller_supplied_tool_metadata','capability_tool':capability_provenance['tool'],
             'spawn_capability_pairs':[{'model':row['model'],'effort':row['effort']} for _,row in sorted(pairs.items())]}
    args=spawn_agent_request(choice,receipt)
    if args is None:
        raise ModelPolicyError('explicit spawn choice failed model policy')
    return {'status':'explicit','spawn_agent':args,
            'dispatch_receipt':compact_dispatch_receipt(args,'explicit',receipt),'receipt':receipt}


def route(request,root,key_file=None,jev_model='jev-latest',*,transport=http_choice):
    started=time.monotonic();receipt={'schema':'CPI236LIMITEDV1','status':'fallback','reason':'native_context_unavailable','attempts':0};pair=None;capability_provenance=None
    try:
        if not isinstance(request,dict):raise ValueError('native context')
        capability_provenance=request.get('capability_provenance')
        if request.get('explicit_choice') is not None:
            if capability_provenance is None:raise ModelPolicyError('explicit spawn choice requires current tool metadata')
            return explicit_spawn_request(request['explicit_choice'],capability_provenance)
        remaining=request['remaining_ms'];task=request['task'];ordinary=request['ordinary']
        if isinstance(remaining,bool)or not isinstance(remaining,(int,float))or not math.isfinite(remaining)or not 0<remaining<=8000 or not isinstance(task,str):raise ValueError('native context')
        deadline=started+remaining/1000
        key=read_credential(key_file)
        if capability_provenance is not None and 'catalog' in request:
            raise ValueError('choose one capability provenance source')
        pair,receipt=choose(task,request.get('catalog'),key,jev_model,deadline,ordinary=ordinary,
                            capability_provenance=capability_provenance,transport=transport)
    except ModelPolicyError:
        return {'status':'refused','receipt':{'status':'refused','reason':'worker_model_policy'}}
    except Exception as error:
        receipt.update(status='fallback',reason='native_bridge_'+type(error).__name__)
    choice_selected = bool(pair and receipt['status']=='selected')
    if choice_selected:
        receipt.update(status='choice_proposed')
        if capability_provenance is None:
            receipt['native_execution_selection']='unconfirmed_pending_native_snapshot_preflight'
    selected_pair=pair if choice_selected else receipt.get('ordinary_worker')
    spawn_args=spawn_agent_request(selected_pair,receipt)
    if spawn_args is not None:
        dispatch_status='selected' if choice_selected else 'ordinary_fallback'
        dispatch_receipt=compact_dispatch_receipt(spawn_args,dispatch_status,receipt)
        receipt['spawn_agent_request']=dispatch_receipt
        if capability_provenance is None:
            receipt['native_execution_selection']=(
                'unconfirmed_pending_native_snapshot_preflight' if choice_selected else 'ordinary_fallback')
        else:
            receipt['spawn_arguments']='ready_not_invoked'
    else:
        dispatch_receipt=None
    receipt.update(preparation_stage='native_prepared_context_choice',elapsed_ms=round((time.monotonic()-started)*1000))
    try:
        work=Path(root)/'work';work.mkdir(exist_ok=True)
        directory=Path(tempfile.mkdtemp(prefix='jev-',dir=work));directory.chmod(0o700)
        path=directory/'receipt.json';path.write_text(json.dumps(receipt,sort_keys=True)+'\n');path.chmod(0o600)
    except Exception:
        return {'status':'fallback','receipt':{'status':'fallback','reason':'native_bridge_receipt_unavailable'}}
    result={'status':'selected' if choice_selected else 'fallback','receipt':receipt}
    if choice_selected:result.update(model=pair['model'],effort=pair['effort'])
    if spawn_args is not None:result.update(spawn_agent=spawn_args,dispatch_receipt=dispatch_receipt)
    return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--credential-file');parser.add_argument('--jev-model',default='jev-latest');args=parser.parse_args()
    raw=sys.stdin.buffer.read(MAX_CONTEXT+1)
    try:
        if len(raw)>MAX_CONTEXT:raise ValueError('context bound')
        request=json.loads(raw)
        result=route(request,args.root,args.credential_file,args.jev_model)
        output=json.dumps(result,separators=(',',':')).encode()
        if len(output)>65536:raise ValueError('response bound')
    except Exception:
        output=b'{"status":"fallback","receipt":{"reason":"native_bridge_protocol"}}'
    sys.stdout.buffer.write(output)

if __name__=='__main__':main()
