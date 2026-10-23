#!/usr/bin/python3
"""One local transient user unit per private job; no shell or scheduler."""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import resource
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
CODE_ROOT=HERE.parents[1]
ENV_KEYS=('HOME','USER','LOGNAME','PATH','LANG','LC_ALL','LC_CTYPE','TMPDIR','XDG_CONFIG_HOME','XDG_CACHE_HOME','XDG_DATA_HOME','CCACHE_DIR','CCACHE_CONFIGPATH','CCACHE_TEMPDIR')
PROPS=('LoadState','ActiveState','SubState','MainPID','InvocationID','ControlGroup','Description','MemoryMax','MemoryHigh','MemorySwapMax','CPUQuotaPerSecUSec','TasksMax','LimitNPROC','LimitNPROCSoft','LimitAS','LimitASSoft','RuntimeMaxUSec','KillMode','Restart','ExecMainCode','ExecMainStatus','Result','Delegate','DelegateSubgroup')

class Refused(Exception):pass
def need(value,code):
    if not value:raise Refused(code)
def unit_description(intent):
    """Derive manager identity from this job's unit, including stored v1 units."""
    jobid=intent.get('job_id');unit=intent.get('unit')
    need(type(jobid) is str and type(unit) is str,'job_identity')
    suffix='-'+jobid+'.service'
    need(unit.endswith(suffix),'job_identity')
    prefix=unit[:-len(suffix)]
    need(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}',prefix) is not None,'job_identity')
    return prefix+' '+jobid
def digest(raw):return hashlib.sha256(raw).hexdigest()
def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':')).encode()
def private_dir(path):
    p=Path(path).absolute();s=p.lstat()
    need(p==p.resolve() and stat.S_ISDIR(s.st_mode) and s.st_uid==os.getuid() and not s.st_mode&0o077,'private_directory')
    return p
def read_bytes(p,private=True):
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as f:
        s=os.fstat(f.fileno());need(stat.S_ISREG(s.st_mode) and s.st_nlink==1,'regular_file')
        if private:need(s.st_uid==os.getuid() and not s.st_mode&0o077,'private_file')
        raw=f.read(1048577);need(len(raw)<=1048576,'file_size')
    return raw
def read(p):return json.loads(read_bytes(p))
def open_input(path,expected=None):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        s=os.fstat(fd);need(stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and s.st_nlink==1 and not s.st_mode&0o077 and s.st_size<=1048576,'private_stdin')
        identity=[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]
        need(expected is None or identity==expected,'stdin_changed')
        return os.fdopen(fd,'rb'),identity
    except BaseException:
        os.close(fd);raise
def publish(p,v):
    raw=canonical(v)+b'\n';tmp=p.parent/('.'+uuid.uuid4().hex+'.part')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        # Linux RENAME_NOREPLACE publishes one link atomically and never replaces history.
        libc=ctypes.CDLL(None,use_errno=True)
        rename=libc.renameat2
        rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        rename.restype=ctypes.c_int
        if rename(-100,os.fsencode(tmp),-100,os.fsencode(p),1)!=0:
            error=ctypes.get_errno();raise OSError(error,os.strerror(error))
        d=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(d)
        finally:os.close(d)
    finally:tmp.unlink(missing_ok=True)
def process(pid):
    p=Path('/proc')/str(pid);fields=(p/'stat').read_text().rsplit(')',1)[1].split()
    return {'pid':pid,'start_ticks':int(fields[19]),'uid':p.stat().st_uid,'cgroup':(p/'cgroup').read_text().strip().split('::',1)[1]}
def cgdir(value):
    if value=='':return None
    need(value.startswith('/') and value!='/' and '..' not in PurePosixPath(value).parts,'cgroup_path')
    return Path('/sys/fs/cgroup')/value.lstrip('/')
TOPOLOGY = 'management-workload-v1'
def service_cgroup(ready):
    return ready.get('topology', {}).get('service', ready['process']['cgroup'])
def management_cgroup(intent, service):
    return service + '/management' if intent.get('topology') == TOPOLOGY else service
def subtree_members(cgroup, recursive=False):
    cg=cgdir(cgroup);need(cg is not None and cg==cg.resolve(), 'missing_cgroup')
    paths=[cg]
    if recursive:
        for parent,dirs,_ in os.walk(cg,followlinks=False):
            for name in dirs:
                path=Path(parent)/name
                need(not path.is_symlink() and path==path.resolve(), 'cgroup_symlink')
                paths.append(path)
    members=[]
    for path in paths:
        try: members.extend((int(pid), cgroup + ('/' + str(path.relative_to(cg)) if path != cg else '')) for pid in (path/'cgroup.procs').read_text().split())
        except FileNotFoundError: continue
    return members
def subtree_empty(cgroup, recursive=False):
    cg=cgdir(cgroup)
    return cg is None or not cg.exists() or not subtree_members(cgroup,recursive)
def topology_check(intent,ready,info):
    if intent.get('topology') != TOPOLOGY:
        need('topology' not in intent and 'topology' not in ready,'unsupported_topology')
        return
    service=service_cgroup(ready);cg=cgdir(service)
    expected={'version':TOPOLOGY,'service':service,'management':service+'/management','workload':service+'/workload'}
    need(ready.get('topology')==expected and ready['process']['cgroup']==expected['management'],'topology_binding')
    need(info.get('Delegate')=='yes' and info.get('DelegateSubgroup')=='management','delegation_manager')
    if cg.exists():
        need(not (cg/'cgroup.procs').read_text().strip(),'nonempty_service_root')
        need('cpu' in (cg/'cgroup.subtree_control').read_text().split(),'cpu_not_delegated')
        need({p.name for p in cg.iterdir() if p.is_dir()}=={'management','workload'},'unexpected_topology')
        for name in ('management','workload'):
            leaf=cg/name
            need(not leaf.is_symlink() and leaf==leaf.resolve() and not any(p.is_dir() for p in leaf.iterdir()),'unexpected_topology')
        need((cg/'management/cpu.max').read_text().split()[0]=='max','management_throttled')
        need((cg/'cgroup.type').read_text().strip()=='domain','cgroup_domain')
def setup_topology(intent,info):
    service=info['ControlGroup'];cg=cgdir(service)
    need(intent.get('topology')==TOPOLOGY and process(os.getpid())['cgroup']==service+'/management','management_placement')
    need(info.get('Delegate')=='yes' and info.get('DelegateSubgroup')=='management','delegation_manager')
    need(not (cg/'cgroup.procs').read_text().strip(),'nonempty_service_root')
    need({p.name for p in cg.iterdir() if p.is_dir()}=={'management'},'unexpected_topology')
    (cg/'cgroup.subtree_control').write_text('+cpu')
    (cg/'workload').mkdir()
    topology={'version':TOPOLOGY,'service':service,'management':service+'/management','workload':service+'/workload'}
    topology_check(intent,{'process':process(os.getpid()),'topology':topology},info)
    return topology
def descendants(pid):
    try: original=process(pid)
    except (FileNotFoundError,ProcessLookupError):return []
    need(original['uid']==os.getuid(),'descendant_uid')
    def birth(member):return (member['pid'],member['uid'],member['start_ticks'])
    result=[];pending=[(pid,original)];seen={birth(original)}
    while pending:
        parent,saved=pending.pop()
        try:
            current=process(parent)
            need(birth(current)==birth(saved),'descendant_parent_birth')
            tasks=list((Path('/proc')/str(parent)/'task').iterdir())
        except (FileNotFoundError,ProcessLookupError):continue
        children=set()
        for task in tasks:
            need(task.name.isdigit(),'descendant_thread_path')
            try: children.update(int(text) for text in (task/'children').read_text().split())
            except (FileNotFoundError,ProcessLookupError):continue
        try: current=process(parent)
        except (FileNotFoundError,ProcessLookupError):current=None
        if current is not None:need(birth(current)==birth(saved),'descendant_parent_birth')
        for child in children:
            try: member=process(child)
            except (FileNotFoundError,ProcessLookupError):continue
            need(member['uid']==os.getuid(),'descendant_uid')
            identity=birth(member)
            if identity in seen:continue
            seen.add(identity);result.append(member);pending.append((child,member))
    return result
def placement_check(topology,supervisor,helper,payload):
    def helper_family():
        if helper is None:return {}
        try: current=process(helper['pid'])
        except (FileNotFoundError,ProcessLookupError):return {}
        need(current==helper,'helper_identity')
        family=[current,*descendants(current['pid'])]
        try: current=process(helper['pid'])
        except (FileNotFoundError,ProcessLookupError):return {}
        need(current==helper,'helper_identity')
        need(all(member['uid']==os.getuid() and member['cgroup']==topology['management'] for member in family),'helper_descendant_placement')
        return {member['pid']:member for member in family}
    allowed={supervisor['pid']:supervisor,**helper_family()}
    if payload is not None:
        try: current=process(payload['pid'])
        except (FileNotFoundError,ProcessLookupError):current=None
        if current is not None:
            need(current==payload,'payload_identity_or_escape')
            need(all(member['cgroup']==topology['workload'] and member['uid']==os.getuid()
                     for member in descendants(current['pid'])),'payload_descendant_placement')
    for pid,group in subtree_members(topology['service'],True):
        try: member=process(pid)
        except (FileNotFoundError,ProcessLookupError):continue
        need(member['uid']==os.getuid() and member['cgroup']==group,'member_identity')
        if group==topology['management']:
            if member!=allowed.get(pid):
                # The helper may fork after the first family snapshot. Revalidate its exact birth.
                allowed.update(helper_family())
                try: current=process(pid)
                except (FileNotFoundError,ProcessLookupError):continue
                need(current==member,'member_identity')
            need(member==allowed.get(pid),'management_descendant_placement')
        else:need(group==topology['workload'],'unexpected_topology')
def enter_workload(cgroup):
    cg=cgdir(cgroup);need(cg==cg.resolve() and cg.name=='workload','workload_path')
    (cg/'cgroup.procs').write_text(str(os.getpid()))
    need(process(os.getpid())['cgroup']==cgroup,'workload_placement')
def command(argv,env):return subprocess.run(argv,env=env,capture_output=True,text=True,timeout=10)
def manager_environment():
    uid=os.getuid();runtime=private_dir('/run/user/'+str(uid))
    need(os.environ.get('XDG_RUNTIME_DIR',str(runtime))==str(runtime),'xdg_runtime_mismatch')
    bus=runtime/'bus';s=bus.lstat();need(stat.S_ISSOCK(s.st_mode) and s.st_uid==uid,'user_bus_socket')
    with socket.socket(socket.AF_UNIX) as peer:
        peer.connect(str(bus));_,peer_uid,_=struct.unpack('3i',peer.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12));need(peer_uid==uid,'user_bus_peer')
    env={k:os.environ[k] for k in ENV_KEYS if k in os.environ}
    env.update(XDG_RUNTIME_DIR=str(runtime),DBUS_SESSION_BUS_ADDRESS='unix:path='+str(bus),PYTHONDONTWRITEBYTECODE='1')
    for method in ('GetConnectionUnixUser','GetConnectionUnixProcessID'):
        p=command(['busctl','--address='+env['DBUS_SESSION_BUS_ADDRESS'],'call','org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus',method,'s','org.freedesktop.systemd1'],env)
        need(p.returncode==0 and p.stdout.startswith('u '),'user_manager_bus')
        value=int(p.stdout.split()[1])
        if method=='GetConnectionUnixUser':need(value==uid,'user_manager_uid')
        else:need(process(value)['uid']==uid,'user_manager_process')
    p=command(['systemctl','--user','show','--property=Version','--value'],env)
    need(p.returncode==0 and p.stdout.strip(),'user_manager_version')
    return env,p.stdout.strip()
def unit_show(unit,env):
    p=command(['systemctl','--user','show',unit,'--no-pager','--property='+','.join(PROPS)],env)
    values=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
    if values.get('LoadState')=='not-found':return values
    need(p.returncode==0 and set(PROPS)<=values.keys(),'manager_query')
    return values
def limits_config(c):
    need(type(c) is dict and set(c)=={'memory_bytes','cpu_percent','address_space_bytes','runtime_seconds'},'unsupported_limit_property')
    need(all(type(value) is str and value=='infinity' for value in c.values()),'finite_limits_unsupported')
    return c
def limits_are_unlimited(c):
    return (type(c) is dict and set(c)=={'memory_bytes','cpu_percent','address_space_bytes','runtime_seconds'}
            and all(type(value) is str and value=='infinity' for value in c.values()))
def configuration(path):
    raw=read_bytes(path);c=json.loads(raw)
    need(type(c) is dict and set(c) in ({'argv','cwd','limits','sources'},{'argv','cwd','limits','sources','stdin_file'}),'unsupported_config_property')
    need(type(c['argv']) is list and c['argv'] and all(type(x) is str and '\0' not in x for x in c['argv']),'argv')
    executable=Path(c['argv'][0]);need(executable.is_absolute() and executable.is_file() and os.access(executable,os.X_OK),'executable')
    need(type(c['cwd']) is str and Path(c['cwd']).is_absolute(),'absolute_cwd')
    private_dir(c['cwd']);limits_config(c['limits'])
    need(type(c['sources']) is dict,'source_map');pins(c['sources'])
    if c.get('stdin_file') is not None:
        need(type(c['stdin_file']) is str and Path(c['stdin_file']).is_absolute(),'absolute_stdin')
        f,_=open_input(c['stdin_file']);f.close()
    return c,digest(raw)
def source_digest(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as f:
        before=os.fstat(f.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_nlink==1,'regular_file')
        def identity(s):
            return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode,s.st_uid,s.st_gid,s.st_nlink)
        hashed=hashlib.sha256();count=0
        remaining=before.st_size
        while remaining:
            block=f.read(min(65536,remaining))
            if not block:break
            hashed.update(block);count+=len(block);remaining-=len(block)
        need(not f.read(1),'source_changed')
        need(identity(before)==identity(os.fstat(f.fileno()))==identity(Path(path).lstat())
             and count==before.st_size,'source_changed')
        return hashed.hexdigest()
def runtime_microseconds(value):
    need(type(value) is str and 0<len(value)<=256,'runtime_manager')
    if re.fullmatch(r'(0|[1-9][0-9]*)',value):
        total=int(value)  # Bare manager integers are microseconds, not seconds.
    else:
        # Whole-second configured limits cannot produce fractional/subsecond formatter output.
        units={'y':31557600,'month':2629800,'w':604800,'d':86400,'h':3600,'min':60,'s':1}
        total=0;previous=None
        parts=value.split(' ');need(len(parts)<=len(units),'runtime_manager')
        for part in parts:
            match=re.fullmatch(r'(0|[1-9][0-9]*)(y|month|w|d|h|min|s)',part)
            need(match is not None,'runtime_manager')
            seconds=units[match[2]];need(previous is None or seconds<previous,'runtime_manager')
            previous=seconds;total+=int(match[1])*seconds*1000000
    need(total<(1<<64)-1,'runtime_manager')
    return total
def runtime_matches(actual,seconds):
    if seconds=='infinity':return actual=='infinity'
    need(type(seconds) is int and seconds>=10,'runtime_budget')
    return runtime_microseconds(actual)==seconds*1000000
def admission_wait_ok(job,cancelled,legacy_deadline,require_launcher=False):
    need(not cancelled and not (job/'stop-intent.json').exists(),'admission_cancelled')
    launch=read(job/'launch-intent.json') if (job/'launch-intent.json').exists() else {}
    if 'launcher_process' not in launch:
        need(time.monotonic()<legacy_deadline,'admission_expired')
        return
    if not require_launcher and (job/'admission.json').exists():return
    original=launch['launcher_process']
    need(type(original) is dict and set(original)=={'pid','uid','start_ticks','cgroup'}
         and type(original['pid']) is int and original['pid']>0
         and type(original['start_ticks']) is int and original['start_ticks']>0
         and original['uid']==os.getuid(),'admission_launcher_identity')
    try:current=process(original['pid'])
    except (FileNotFoundError,ProcessLookupError):current=None
    need(current==original,'admission_launcher_lost')
def wait_ready(job,intent,env):
    launch=read(job/'launch-intent.json')
    need('launcher_process' in launch,'readiness_launcher_identity')
    invocation=None;supervisor=None;cgroup=None
    while True:
        admission_wait_ok(job,False,float('inf'),require_launcher=True)
        info=unit_show(intent['unit'],env)
        need(info.get('LoadState')=='loaded' and info.get('ActiveState') in ('active','activating')
             and info.get('Result') in ('success',''),'readiness_unit_failed')
        need(info.get('Description')==unit_description(intent) and bool(info.get('InvocationID')),
             'readiness_unit_identity')
        if invocation is None:invocation=info['InvocationID']
        need(info['InvocationID']==invocation,'readiness_unit_identity')
        current_cg=info.get('ControlGroup','')
        if current_cg:
            need(current_cg.endswith('/'+intent['unit']) and cgdir(current_cg) is not None,
                 'readiness_unit_identity')
            if cgroup is None:cgroup=current_cg
            need(cgroup==current_cg,'readiness_unit_identity')
        pid=int(info['MainPID'])
        if pid:
            try:current=process(pid)
            except (FileNotFoundError,ProcessLookupError):current=None
            need(current is not None and current['uid']==intent['uid'] and current_cg
                 and current['cgroup']==management_cgroup(intent,current_cg),'readiness_supervisor_lost')
            if supervisor is None:supervisor=current
            need(current==supervisor,'readiness_supervisor_lost')
        else:
            need(supervisor is None and info['ActiveState']=='activating','readiness_supervisor_lost')
        if (job/'ready.json').exists():
            need(info['ActiveState']=='active' and info.get('SubState')=='running' and pid>0,
                 'readiness_supervisor_lost')
            return
        time.sleep(.025)
def pins(values):
    for name,expected in values.items():
        need(type(name) is str and Path(name).is_absolute() and type(expected) is str and source_digest(Path(name))==expected,'source_changed')
def source_pins():
    return {str(p):source_digest(p) for p in [HERE/'runner.py',CODE_ROOT/'scripts/project-task-runner',Path(sys.executable).resolve()]}
def historical_budget_check(intent,ready,info):
    limit=intent['config']['limits'];cg=cgdir(ready['process']['cgroup']);need(cg is not None,'missing_cgroup')
    actual={n:(cg/n).read_text().strip() for n in ('memory.max','memory.swap.max','cpu.max')}
    need(actual['memory.max']==str(limit['memory_bytes']) and actual['memory.swap.max']=='0','physical_budget')
    quota,period=map(int,actual['cpu.max'].split());need(quota*100==period*limit['cpu_percent'],'cpu_budget')
    as_value=resource.RLIM_INFINITY if limit['address_space_bytes']=='infinity' else limit['address_space_bytes']
    need(ready['address_space']==[as_value,as_value],'address_space_actual')
    wanted_as='infinity' if as_value==resource.RLIM_INFINITY else str(as_value)
    need(info['LimitAS']==info['LimitASSoft']==wanted_as,'address_space_manager')
    need(runtime_matches(info['RuntimeMaxUSec'],limit['runtime_seconds']),'runtime_manager')
    need(info['MemoryMax']==str(limit['memory_bytes']) and info['MemorySwapMax']=='0' and info['KillMode']=='mixed' and info['Restart']=='no','manager_budget')
    return actual
def unlimited_properties():
    return {'MemoryMax':'infinity','MemoryHigh':'infinity','MemorySwapMax':'infinity','CPUQuota':'',
            'TasksMax':'infinity','LimitNPROC':'infinity','LimitAS':'infinity','RuntimeMaxSec':'infinity',
            'TimeoutStopSec':5,'KillMode':'mixed','Restart':'no','UMask':'0077',
            'StandardOutput':'null','StandardError':'null'}
def budget_check(intent,ready,info):
    limit=intent.get('config',{}).get('limits',{})
    if limits_are_unlimited(limit):
        pass
    elif (type(limit) is dict and set(limit)=={'memory_bytes','cpu_percent','address_space_bytes','runtime_seconds'}
          and type(limit['memory_bytes']) is int and limit['memory_bytes']>=16777216
          and type(limit['cpu_percent']) is int and limit['cpu_percent']>0
          and (limit['address_space_bytes']=='infinity' or type(limit['address_space_bytes']) is int and limit['address_space_bytes']>0)
          and (limit['runtime_seconds']=='infinity' or type(limit['runtime_seconds']) is int and limit['runtime_seconds']>=10)):
        return historical_budget_check(intent,ready,info)
    else:
        need(False,'finite_limits_unsupported')
    groups=[ready['process']['cgroup']]
    if intent.get('topology')==TOPOLOGY:
        topology=ready.get('topology',{})
        need(set(topology)=={'version','service','management','workload'} and topology['version']==TOPOLOGY,'topology_binding')
        groups=[topology[name] for name in ('service','management','workload')]
    actual={}
    for value in groups:
        cg=cgdir(value);need(cg is not None,'missing_cgroup')
        fields={name:(cg/name).read_text().strip() for name in ('memory.max','memory.high','memory.swap.max','pids.max','cpu.max')}
        need(all(fields[name]=='max' for name in ('memory.max','memory.high','memory.swap.max','pids.max')),'physical_budget')
        quota=fields['cpu.max'].split()
        need(len(quota)==2 and quota[0]=='max' and quota[1].isascii() and quota[1].isdecimal() and int(quota[1])>0,'cpu_budget')
        actual[value]=fields
    actual=actual[ready['process']['cgroup']]
    infinity=resource.RLIM_INFINITY
    need(ready['address_space']==[infinity,infinity] and ready['process_limit']==[infinity,infinity],'process_limit_actual')
    need(info['LimitAS']==info['LimitASSoft']=='infinity','address_space_manager')
    need(info['RuntimeMaxUSec']=='infinity','runtime_manager')
    need(all(info[name]=='infinity' for name in ('MemoryMax','MemoryHigh','MemorySwapMax','CPUQuotaPerSecUSec','TasksMax','LimitNPROC','LimitNPROCSoft'))
         and info['KillMode']=='mixed' and info['Restart']=='no','manager_budget')
    return actual
def load_job(job):
    job=private_dir(job);intent=read(job/'intent.json')
    jobid=intent['job_id'];need(type(jobid) is str and len(jobid)==32 and all(x in '0123456789abcdef' for x in jobid),'job_id')
    need(intent['unit'].endswith('-'+jobid+'.service') and unit_description(intent)
         and intent['job_dir']==str(job) and intent['uid']==os.getuid(),'job_identity')
    return job,intent
def custody(job,intent,info):
    ready=read(job/'ready.json')
    need(ready['intent_sha256']==digest(read_bytes(job/'intent.json')) and ready['job_id']==intent['job_id'],'intent_binding')
    need(info.get('LoadState')!='not-found','unit_absent')
    need(info['InvocationID']==ready['invocation_id'] and info['Description']==unit_description(intent),'invocation_mismatch')
    cg=info['ControlGroup']
    need(cg=='' or cg==service_cgroup(ready),'cgroup_mismatch')
    pid=int(info['MainPID'])
    if pid:need(cg==service_cgroup(ready) and process(pid)==ready['process'],'process_mismatch')
    else:
        try:past=process(ready['process']['pid'])
        except (FileNotFoundError,ProcessLookupError):past=None
        need(past is None or any(past[k]!=ready['process'][k] for k in ('pid','uid','start_ticks')),'supervisor_still_present')
    topology_check(intent,ready,info)
    if (job/'custody.json').exists():
        saved=read(job/'custody.json');need(saved['invocation_id']==ready['invocation_id'] and saved['process']==ready['process'],'custody_mismatch')
    return ready
def public_status(job,env=None):
    job,intent=load_job(job);env=manager_environment()[0] if env is None else env
    result=read(job/'result.json') if (job/'result.json').exists() else None
    payload=read(job/'payload-exit.json') if (job/'payload-exit.json').exists() else None
    if payload:need(payload['job_id']==intent['job_id'] and payload['intent_sha256']==digest(read_bytes(job/'intent.json')),'payload_exit_binding')
    info=unit_show(intent['unit'],env)
    if info.get('LoadState')=='not-found':
        if result is None:
            need(payload is not None,'unknown_unit')
            return {'job_id':intent['job_id'],'unit':intent['unit'],'state':'UNKNOWN_TERMINAL','payload_returncode':payload['returncode'],'first_failure':payload['first_failure'],'cleanup':None,'unit_state':'ABSENT','identity_verified':False}
        need((job/'cleanup.json').exists(),'unknown_cleanup')
        ready=read(job/'ready.json')
        if intent.get('topology')==TOPOLOGY:
            need(subtree_empty(service_cgroup(ready),True),'terminal_subtree_live')
        need(result['job_id']==intent['job_id'] and result['intent_sha256']==digest(read_bytes(job/'intent.json')),'result_binding')
        need(result['invocation_id']==read(job/'ready.json')['invocation_id'],'result_invocation')
        return {'job_id':intent['job_id'],'unit':intent['unit'],'state':result['state'],'payload_returncode':result['payload_returncode'],'first_failure':result['first_failure'],'cleanup':result['cleanup'],'unit_state':'ABSENT','identity_verified':True}
    ready=custody(job,intent,info)
    if result:
        need(result['job_id']==intent['job_id'] and result['intent_sha256']==digest(read_bytes(job/'intent.json')) and result['invocation_id']==ready['invocation_id'],'result_binding')
    terminal=info['ActiveState'] in ('inactive','failed') or int(info['MainPID'])==0
    state=result['state'] if result else ('UNKNOWN_TERMINAL' if terminal else ('RUNNING' if (job/'admission.json').exists() else 'WAITING_ADMISSION'))
    return {'job_id':intent['job_id'],'unit':intent['unit'],'state':state,'payload_returncode':result['payload_returncode'] if result else (payload['returncode'] if payload else None),'first_failure':result['first_failure'] if result else (payload['first_failure'] if payload else None),'cleanup':result['cleanup'] if result else None,'unit_state':info['ActiveState']+'/'+info['SubState'],'manager_result':info['Result'],'manager_exit_status':int(info['ExecMainStatus']),'identity_verified':True}
def launch(job,config):
    env,version=manager_environment();c,chash=configuration(config)
    job=Path(job).absolute();private_dir(job.parent);job.mkdir(mode=0o700)
    jobid=uuid.uuid4().hex;intent={'job_id':jobid,'unit':'project-task-'+jobid+'.service','job_dir':str(job),'uid':os.getuid(),'config':c,'config_sha256':chash,'argv_sha256':digest(canonical(c['argv'])),'source_pins':source_pins(),'environment_sha256':digest(canonical(env)),'manager_version':version}
    if c.get('stdin_file'):
        f,intent['stdin_identity']=open_input(c['stdin_file']);f.close()
    publish(job/'environment.json',env);publish(job/'intent.json',intent)
    properties=unlimited_properties()
    argv=['systemd-run','--user','--quiet','--unit='+intent['unit'],'--description='+unit_description(intent),'--service-type=exec','--remain-after-exit','--expand-environment=no','--working-directory='+str(job)]
    for k,v in properties.items():argv.append('--property='+k+'='+str(v))
    for k,v in env.items():argv.append('--setenv='+k+'='+v)
    argv += [sys.executable,'-B',str(HERE/'runner.py'),'_worker',str(job)]
    publish(job/'launch-intent.json',{'job_id':jobid,'properties':properties,'launcher_process':process(os.getpid())})
    try:p=command(argv,env)
    except subprocess.TimeoutExpired:
        publish(job/'launch-outcome.json',{'state':'UNKNOWN','error':'launcher_timeout'});return {'state':'UNKNOWN','job_id':jobid}
    publish(job/'launch-outcome.json',{'state':'RETURNED','exit':p.returncode})
    need(p.returncode==0,'manager_launch_rejected')
    wait_ready(job,intent,env)
    info=unit_show(intent['unit'],env);ready=custody(job,intent,info);actual=budget_check(intent,ready,info)
    need(ready['environment_sha256']==intent['environment_sha256'],'environment_changed');pins(intent['source_pins']);pins(c['sources'])
    publish(job/'custody.json',{'invocation_id':ready['invocation_id'],'process':ready['process'],'cgroup':ready['process']['cgroup'],'budget':actual,'manager':info})
    # Large source validation must not admit a supervisor that already expired.
    info=unit_show(intent['unit'],env);custody(job,intent,info)
    need(info.get('MainPID')==str(ready['process']['pid']) and info.get('ActiveState')=='active'
         and info.get('SubState')=='running','admission_expired')
    publish(job/'admission.json',{'intent_sha256':digest(read_bytes(job/'intent.json')),'invocation_id':ready['invocation_id']})
    return public_status(job,env)
def stop(job):
    job,intent=load_job(job);env=manager_environment()[0]
    fd=os.open(job/'control.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    s=os.fstat(fd)
    if not (stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and s.st_nlink==1 and not s.st_mode&0o077):
        os.close(fd);raise Refused('control_lock')
    with os.fdopen(fd,'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        info=unit_show(intent['unit'],env)
        if info.get('LoadState')=='not-found':return public_status(job,env)
        custody(job,intent,info)
        if not (job/'stop-intent.json').exists():publish(job/'stop-intent.json',{'job_id':intent['job_id']})
        # Revalidate invocation/birth directly before asking the manager to stop this exact unit.
        custody(job,intent,unit_show(intent['unit'],env))
        p=command(['systemctl','--user','stop',intent['unit']],env);need(p.returncode==0,'stop_failed')
        final=unit_show(intent['unit'],env);cg=cgdir(final.get('ControlGroup',''))
        need(final.get('ActiveState') in ('inactive','failed',None) and subtree_empty(final.get('ControlGroup',''),intent.get('topology')==TOPOLOGY),'stop_not_quiescent')
        if not (job/'stop-result.json').exists():publish(job/'stop-result.json',{'unit_absent':final.get('LoadState')=='not-found','quiescent':True})
        return public_status(job,env)
def drain(pipe,log,kept):
    try:chunk=os.read(pipe.fileno(),65536)
    except BlockingIOError:return kept
    if chunk and kept<65536:part=chunk[:65536-kept];log.write(part);kept+=len(part)
    return kept
def signal_members(cgroup,signum,recursive=False):
    for pid,group in subtree_members(cgroup,recursive):
        if pid==os.getpid():continue
        try:
            fd=os.pidfd_open(pid)
            try:
                member=process(pid);need(member['uid']==os.getuid() and member['cgroup']==group,'member_identity')
                signal.pidfd_send_signal(fd,signum)
            finally:os.close(fd)
        except (ProcessLookupError,FileNotFoundError):pass
def cleanup_members(cgroup,child,recursive=False,expected_birth=None):
    errors=[]
    try:
        deadline=time.monotonic()+2
        while True:
            if expected_birth:
                for descendant in descendants(os.getpid()):
                    need(descendant['uid']==os.getuid() and (descendant['cgroup']==cgroup or recursive and descendant['cgroup'].startswith(cgroup+'/')),'descendant_escaped')
                try: current=process(expected_birth['pid'])
                except (ProcessLookupError,FileNotFoundError):current=None
                if current and all(current[k]==expected_birth[k] for k in ('uid','start_ticks')):
                    need(current['cgroup']==expected_birth['cgroup'],'payload_escaped')
            signal_members(cgroup,signal.SIGTERM if time.monotonic()<deadline else signal.SIGKILL,recursive)
            while True:
                try:
                    pid,_=os.waitpid(-1,os.WNOHANG)
                    if pid==0:break
                except ChildProcessError:break
            if not any(pid!=os.getpid() for pid,_ in subtree_members(cgroup,recursive)):break
            need(time.monotonic()<deadline+1,'children_not_quiescent');time.sleep(.025)
        if child is not None and child.returncode is None:child.wait(timeout=1)
    except Exception as e:errors.append(str(e) if isinstance(e,Refused) else type(e).__name__)
    return {'quiescent':not errors,'errors':errors}
def result_record(intent,intent_sha,invocation,code,first,cancelled,cleanup,stop_requested):
    if first is None and not cleanup['quiescent']:first={'phase':'cleanup','code':'failed'}
    state='CANCELLED' if cancelled and stop_requested else ('FAILED' if first else 'SUCCEEDED')
    return {'job_id':intent['job_id'],'intent_sha256':intent_sha,'invocation_id':invocation,'state':state,'payload_returncode':code,'first_failure':first,'cleanup':cleanup}
def worker(job):
    os.umask(0o077);job,intent=load_job(job);env=read(job/'environment.json');c=intent['config']
    child=None;first=None;code=None;cancelled=False;cancelled_at=None
    def cancel(signum,frame):
        nonlocal cancelled,cancelled_at
        cancelled=True
        if cancelled_at is None:cancelled_at=time.monotonic()
    signal.signal(signal.SIGTERM,cancel);signal.signal(signal.SIGINT,cancel)
    cg=process(os.getpid())['cgroup']
    # Internal mode is not an authority: refuse outside this exact manager-owned unit,
    # before writing receipts or touching any cgroup member.
    need(cg.endswith('/'+intent['unit']),'supervisor_cgroup')
    info=unit_show(intent['unit'],env)
    need(info.get('MainPID')==str(os.getpid()) and info.get('ControlGroup')==cg
         and info.get('InvocationID')==os.environ.get('INVOCATION_ID')
         and bool(info.get('InvocationID')) and info.get('Description')==unit_description(intent),'supervisor_unit_identity')
    try:
        need(ctypes.CDLL(None).prctl(36,1,0,0,0)==0,'subreaper')
        need(all(os.environ.get(k)==v for k,v in env.items()),'unit_environment')
        ready={'job_id':intent['job_id'],'intent_sha256':digest(read_bytes(job/'intent.json')),'process':process(os.getpid()),'invocation_id':os.environ['INVOCATION_ID'],'address_space':list(resource.getrlimit(resource.RLIMIT_AS)),'process_limit':list(resource.getrlimit(resource.RLIMIT_NPROC)),'environment_sha256':digest(canonical(env))}
        publish(job/'ready.json',ready)
        deadline=time.monotonic()+6
        while not (job/'admission.json').exists():
            admission_wait_ok(job,cancelled,deadline);time.sleep(.025)
        admission_wait_ok(job,cancelled,deadline)
        a=read(job/'admission.json');need(a=={'intent_sha256':ready['intent_sha256'],'invocation_id':ready['invocation_id']},'admission_identity')
        pins(intent['source_pins']);pins(c['sources']);need(not cancelled,'cancelled_before_payload')
        stdin=open_input(c['stdin_file'],intent['stdin_identity'])[0] if c.get('stdin_file') else subprocess.DEVNULL
        with (job/'payload.log').open('xb') as log:
            try:child=subprocess.Popen(c['argv'],cwd=c['cwd'],env=env,stdin=stdin,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
            finally:
                if stdin!=subprocess.DEVNULL:stdin.close()
            try:birth=process(child.pid)
            except (FileNotFoundError,ProcessLookupError):birth=None
            publish(job/'payload-start.json',{'pid':child.pid,'process':birth,'identity_sampled':birth is not None})
            os.set_blocking(child.stdout.fileno(),False);kept=0
            while child.poll() is None:
                kept=drain(child.stdout,log,kept)
                if cancelled:signal_members(cg,signal.SIGTERM if time.monotonic()-cancelled_at<.5 else signal.SIGKILL)
                time.sleep(.025)
            code=child.wait()
            # Persist the actual first payload result before optional cgroup/log/cleanup work.
            if code!=0:first={'phase':'payload','code':'exit','returncode':code}
            publish(job/'payload-exit.json',{'job_id':intent['job_id'],'intent_sha256':digest(read_bytes(job/'intent.json')),'returncode':code,'first_failure':first})
            kept=drain(child.stdout,log,kept);child.stdout.close()
    except Exception as e:
        if first is None:first={'phase':'supervisor','code':str(e) if isinstance(e,Refused) else type(e).__name__}
        if not (job/'first-failure.json').exists():publish(job/'first-failure.json',first)
    cleanup=cleanup_members(cg,child)
    publish(job/'cleanup.json',cleanup)
    publish(job/'result.json',result_record(intent,digest(read_bytes(job/'intent.json')),os.environ.get('INVOCATION_ID'),code,first,cancelled,cleanup,(job/'stop-intent.json').exists()))
    return 0
def main():
    os.umask(0o077)
    if len(sys.argv)>1 and sys.argv[1]=='_worker':return worker(sys.argv[2])
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='operation',required=True)
    for name in ('run','status','stop'):
        p=sub.add_parser(name);p.add_argument('--job',required=True)
        if name=='run':p.add_argument('--config',required=True)
    args=parser.parse_args()
    try:
        result=launch(args.job,Path(args.config)) if args.operation=='run' else (stop(args.job) if args.operation=='stop' else public_status(args.job))
        print(json.dumps(result,sort_keys=True));return 0
    except Exception as e:
        print(json.dumps({'state':'UNKNOWN' if args.operation!='run' else 'REFUSED','error':str(e) if isinstance(e,Refused) else type(e).__name__},sort_keys=True));return 2

if __name__=='__main__':raise SystemExit(main())
