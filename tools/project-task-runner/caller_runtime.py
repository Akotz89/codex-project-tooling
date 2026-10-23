"""Generic bounded caller control, process capture, and promotion primitives.

Project adapters configure the runner contract once, then supply project-owned
paths and custody callbacks. No project packet or profile semantics live here.
"""
import hashlib
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path

base = None
need = None


def configure(runner, refusal):
    """Bind the package's checked runner primitives for this caller process."""
    global base, need
    base, need = runner, refusal


class ControlContention(Exception):
    """Only bounded exhaustion after a validated unlinked-control snapshot."""


class ControlReadDelay(Exception):
    """A stable private snapshot still needs identity/allocation checks before yield."""
    def __init__(self,value):
        super().__init__('control_read_delay');self.value=value


def read_control(path,deadline=None):
    """Reopen transient unlinked snapshots; accept only stable private files."""
    path=Path(path);until=min(time.monotonic()+.1,deadline if deadline is not None else float('inf'))
    private=lambda info:stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o077
    identity=lambda info:(info.st_dev,info.st_ino)
    stable=lambda info:(info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    for attempt in range(3):
        now=time.monotonic();need(deadline is None or now<deadline,'build_deadline')
        if attempt and now>=until:raise ControlContention('control_replacement_retry_exhausted')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno());need(private(before),'private_control')
            if before.st_nlink==0:
                current=path.lstat()
                need(private(current) and current.st_nlink in (0,1) and (current.st_nlink==0 or identity(current)!=identity(before)),'control_replacement')
                time.sleep(0);continue
            need(before.st_nlink==1,'regular_file')
            raw=stream.read(1048577);need(len(raw)<=1048576,'file_size')
            after=os.fstat(stream.fileno());current=path.lstat()
            if identity(current)!=identity(before):
                observed=os.fstat(stream.fileno())
                need(private(current) and current.st_nlink in (0,1) and observed.st_nlink==0,'control_replacement')
                time.sleep(0);continue
            if not (after.st_nlink==current.st_nlink==1 and stable(before)==stable(after)==stable(current)):
                observed=os.fstat(stream.fileno());latest=path.lstat()
                if private(observed) and observed.st_nlink==0 and private(latest) and latest.st_nlink in (0,1) and (latest.st_nlink==0 or identity(latest)!=identity(before)):
                    time.sleep(0);continue
                fields=lambda info:[info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns]
                need(False,'control_changed '+json.dumps({name:fields(info) for name,info in [('before',before),('after',after),('current',current),('observed',observed),('latest',latest)]},separators=(',',':')))
            need(0<=time.time()-after.st_mtime<=10,'stale_control')
            value=json.loads(raw)
            now=time.monotonic();need(deadline is None or now<deadline,'build_deadline')
            if now>=until:raise ControlReadDelay(value)
            return value
    need(deadline is None or time.monotonic()<deadline,'build_deadline')
    raise ControlContention('control_replacement_retry_exhausted')


class Control:
    def __init__(self,path,job_id,deadline,max_jobs,check=lambda:None,*,cpu_ceiling=None):
        self.path,self.job_id,self.deadline,self.max_jobs,self.check=Path(path),job_id,deadline,max_jobs,check
        self.cpu_ceiling=max_jobs if cpu_ceiling is None else cpu_ceiling
    def slots(self):
        need(self.deadline is None or time.monotonic()<self.deadline,'build_deadline')
        delayed=False
        try:
            self.check();value=read_control(self.path,self.deadline)
        except ControlContention:
            return 0
        except ControlReadDelay as error:
            value=error.value;delayed=True
        need(set(value)=={'version','job_id','cpu','fanout','checkpoint'} and value['version']==2
             and value['job_id']==self.job_id,'control_identity')
        need(type(value['fanout']) is int and 0<=value['fanout']<=self.max_jobs
             and type(value['checkpoint']) is bool and type(value['cpu']) in (int,float)
             and math.isfinite(value['cpu']) and 0<=value['cpu']<=self.cpu_ceiling
             and (value['cpu']>0 or value['fanout']==0 or value['checkpoint']),'control_allocation')
        return 0 if delayed or value['checkpoint'] or value['cpu']==0 else value['fanout']
    def ready(self):
        while not self.slots():time.sleep(.02)
    def check_running(self):
        self.slots()  # Never block pipe drainage or stop an active compiler merely for a smaller allocation.


def capture(child,stdin_file=None,*,check=lambda:None,logs,project_root,owned_root):
    """Drain a child without a useful-work deadline, preserving bounded memory use."""
    proc=child.proc;selector=selectors.DefaultSelector();pidfd=None;source=None;outputs={}
    seen={'stdout':0,'stderr':0};eof={'stdout':False,'stderr':False};offset=0
    identity=None;first=None;stdin_error=None;input_identity=None
    def status():
        paths=logs if type(logs) is dict else {}
        stdout=paths.get('stdout');stderr=paths.get('stderr')
        return dict(pid=child.pid,identity=identity,seen=seen.copy(),eof=eof.copy(),
                    stdout=None if stdout is None else str(stdout),
                    stderr=None if stderr is None else str(stderr),
                    stdin=dict(bytes=None if input_identity is None else input_identity[2],
                               delivered=offset,complete=(stdin_file is None or
                                                          input_identity is not None and offset==input_identity[2]),
                               error=stdin_error),error=None if first is None else type(first).__name__)
    try:
        need(type(logs) is dict and set(logs)=={'stdout','stderr'},'capture_logs')
        project_root=Path(project_root).resolve(strict=True)
        owned_root=Path(owned_root).resolve(strict=True)
        need(owned_root.is_relative_to(project_root),'capture_owned_root')
        if stdin_file is not None:
            stdin_path=Path(stdin_file)
            need(stdin_path.is_absolute() and stdin_path.resolve(strict=True)==stdin_path
                 and stdin_path.is_relative_to(project_root),'stdin_project_root')
            fd=os.open(stdin_path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
            source=os.fdopen(fd,'rb');info=os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1
                 and not info.st_mode&0o077,'private_stdin')
            input_identity=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
        for name,path in logs.items():
            log_path=Path(path)
            need(log_path.is_absolute() and log_path==log_path.resolve()
                 and log_path.parent.resolve(strict=True)==log_path.parent
                 and log_path.is_relative_to(owned_root),'capture_log_owner_root')
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
            outputs[name]=os.fdopen(fd,'wb')
        pidfd=os.pidfd_open(child.pid);identity=base.process(child.pid);parent=base.process(os.getpid())
        need(identity['uid']==parent['uid'] and identity['cgroup']==parent['cgroup'],'child_custody')
        for name,pipe in (('stdout',proc.stdout),('stderr',proc.stderr)):
            os.set_blocking(pipe.fileno(),False);selector.register(pipe,selectors.EVENT_READ,name)
        if source is not None:
            os.set_blocking(proc.stdin.fileno(),False);selector.register(proc.stdin,selectors.EVENT_WRITE,'stdin')
        else:proc.stdin.close()
        pending=b''
        while selector.get_map() or not child.reaped:
            check()
            if not child.reaped and child.exited():child.finish()
            for key,_ in selector.select(.02):
                if key.data=='stdin':
                    if not pending:pending=source.read(65536)
                    if not pending:
                        after=os.fstat(source.fileno())
                        need((after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)==input_identity
                             and offset==input_identity[2],'stdin_changed')
                        selector.unregister(key.fileobj);key.fileobj.close();source.close();source=None;continue
                    try:
                        written=os.write(key.fileobj.fileno(),pending)
                        need(written>0,'short_stdin_write');pending=pending[written:];offset+=written
                    except BrokenPipeError:
                        stdin_error='BrokenPipeError';selector.unregister(key.fileobj);key.fileobj.close();continue
                else:
                    name=key.data;chunk=os.read(key.fileobj.fileno(),65536)
                    if not chunk:
                        eof[name]=True;selector.unregister(key.fileobj);key.fileobj.close();continue
                    written=outputs[name].write(chunk)
                    need(written==len(chunk),'short_output_write')
                    outputs[name].flush();seen[name]+=written
        for output in outputs.values():output.flush();os.fsync(output.fileno())
        result=subprocess.CompletedProcess(proc.args,child.returncode,None,None)
        result.capture=status();need(stdin_error is None,'partial_stdin_delivery')
        need(all(eof.values()),'incomplete_child_eof')
        return result
    except BaseException as error:
        first=error;error.capture=status();error.add_note('stream capture '+json.dumps(error.capture));raise
    finally:
        try:child.finish()
        except BaseException as error:
            if first is None:raise
            first.add_note('Child cleanup failed: '+type(error).__name__)
        finally:
            for pipe in (proc.stdin,proc.stdout,proc.stderr):
                if pipe is not None:pipe.close()
            for output in outputs.values():output.close()
            if source is not None:source.close()
            selector.close()
            if pidfd is not None:os.close(pidfd)


class Promotion:
    """Exclusive no-overwrite copies with actual counters and recovery evidence."""
    def __init__(self,source,destination,byte_limit,inode_limit,check=lambda:None,peer=None,scratch_usage=None,total_limits=None,*,owner_root=None):
        self.source=base.private_dir(source);self.destination=Path(destination)
        owner_root=Path.cwd().resolve(strict=True) if owner_root is None else Path(owner_root).resolve(strict=True)
        need(self.source.is_relative_to(owner_root) and self.destination.is_absolute()
             and self.destination.is_relative_to(owner_root),'promotion_owner_root')
        need((byte_limit,inode_limit)==(None,None) and total_limits is None,'promotion_must_not_impose_quotas')
        need(self.destination.is_absolute() and self.destination==self.destination.resolve()
             and not self.destination.exists(),'promotion_destination_exists')
        base.private_dir(self.destination.parent)
        need(not self.destination.is_relative_to(self.source) and not self.source.is_relative_to(self.destination),'promotion_overlap')
        self.check,self.peer=check,peer
        self.scratch_usage=scratch_usage or {}
        if peer is not None:peer.peer=self
        self.logical=self.allocated=self.inodes=0;self.peak_logical=self.peak_allocated=self.peak_inodes=0
        self.rows=[];self.partial=None;self.entries={};self.manifest=None;self.complete=False
        self.check();self.destination.mkdir(mode=0o700);self.sync(self.destination.parent)
        self.track(self.destination);self.recover()
    def track(self,path,removed=False):
        existed=str(path) in self.entries;old=self.entries.pop(str(path),(0,0))
        if existed:self.inodes-=1
        self.logical-=old[0];self.allocated-=old[1]
        if not removed:
            info=path.lstat();need(not stat.S_ISLNK(info.st_mode),'promotion_alias')
            row=(info.st_size if stat.S_ISREG(info.st_mode) else 0,info.st_blocks*512)
            self.entries[str(path)]=row;self.inodes+=1;self.logical+=row[0];self.allocated+=row[1]
        self.peak_logical=max(self.peak_logical,self.logical);self.peak_allocated=max(self.peak_allocated,self.allocated)
        self.peak_inodes=max(self.peak_inodes,self.inodes)
    def measure(self):
        """One final or failed-copy census verifies incremental accounting."""
        paths=[self.destination]
        for directory,dirs,files in os.walk(self.destination,followlinks=False):
            paths.extend(Path(directory)/name for name in dirs+files)
        measured={}
        for path in paths:
            info=path.lstat();need(not stat.S_ISLNK(info.st_mode),'promotion_alias')
            measured[str(path)]=(info.st_size if stat.S_ISREG(info.st_mode) else 0,info.st_blocks*512)
        self.entries=measured;self.logical=sum(v[0] for v in measured.values())
        self.allocated=sum(v[1] for v in measured.values());self.inodes=len(measured)
        self.peak_logical=max(self.peak_logical,self.logical);self.peak_allocated=max(self.peak_allocated,self.allocated)
        self.peak_inodes=max(self.peak_inodes,self.inodes)
    def budget(self,extra=0,inodes=0):
        self.check()
    def reserve(self,value):
        self.check()
    def recover(self,check=True):
        if check:self.check()
        target=self.destination/'recovery.json';temp=self.destination/'.recovery.part'
        need(not temp.exists(),'promotion_recovery_partial')
        value=self.receipt();value['files_count']=len(self.rows);value['files']=self.rows[-1:]
        self.reserve(value)
        # Replacement is limited to our existing recovery record; copies never overwrite.
        self.publish(temp,value,check=check)
        os.replace(temp,target);self.track(temp,removed=True);self.track(target);self.track(self.destination)
        self.budget();self.sync(self.destination)
    @staticmethod
    def sync(path):
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
    def copy(self,relative):
        rel=Path(relative)
        need(not rel.is_absolute() and rel.parts and not any(x in ('.','..') for x in rel.parts)
             and rel.parts[0] not in ('recovery.json','.recovery.part','manifest.json'),'promotion_relative')
        src=self.source/rel;dest=self.destination/rel
        need(src.resolve()==src,'promotion_source_alias')
        before=src.lstat();need(stat.S_ISREG(before.st_mode) and before.st_nlink==1,'promotion_source_type')
        self.budget()
        parent=self.destination
        for name in rel.parts[:-1]:
            previous=parent;parent=parent/name
            if not parent.exists():
                self.check();parent.mkdir(mode=0o700);self.sync(previous);self.track(parent);self.track(previous)
            base.private_dir(parent)
        self.partial={'path':str(rel),'bytes_copied':0};self.recover()
        identity=lambda s:(s.st_dev,s.st_ino,s.st_mode,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        source_fd=os.open(src,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            need(identity(os.fstat(source_fd))==identity(before),'promotion_source_changed')
            digest=hashlib.sha256();copied=0
            fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,stat.S_IMODE(before.st_mode))
            self.track(dest);self.track(dest.parent)
            with os.fdopen(fd,'wb') as output:
                while chunk:=os.read(source_fd,65536):
                    self.check();need(copied+len(chunk)<=before.st_size,'promotion_source_grew')
                    written=output.write(chunk);need(written==len(chunk),'short_promotion_write')
                    output.flush();copied+=written;digest.update(chunk)
                    self.partial['bytes_copied']=copied;self.track(dest);self.budget()
                output.flush();os.fchmod(output.fileno(),stat.S_IMODE(before.st_mode))
                need(stat.S_IMODE(os.fstat(output.fileno()).st_mode)==stat.S_IMODE(before.st_mode),'promotion_mode')
                os.fsync(output.fileno())
            need(identity(os.fstat(source_fd))==identity(before)==identity(src.lstat()) and copied==before.st_size,'promotion_source_changed')
            self.sync(dest.parent);self.track(dest)
            info=dest.stat();row={'path':str(rel),'bytes':copied,'allocated_bytes':info.st_blocks*512,
                                'sha256':digest.hexdigest(),'mode':oct(stat.S_IMODE(info.st_mode))}
            self.rows.append(row);self.partial=None;self.recover();return row
        except BaseException as error:
            try:self.measure()
            except BaseException as diagnostic:error.add_note('Promotion census failed: '+type(diagnostic).__name__)
            try:self.recover(check=False)
            except BaseException as diagnostic:error.add_note('Recovery publication failed: '+type(diagnostic).__name__)
            raise
        finally:os.close(source_fd)
    def tree(self,relative):
        start=self.source/relative;need(start.resolve()==start and start.is_dir(),'promotion_tree')
        for directory,dirs,files in os.walk(start,followlinks=False):
            need(all(not (Path(directory)/n).is_symlink() for n in dirs),'promotion_directory_alias')
            for name in sorted(files):self.copy((Path(directory)/name).relative_to(self.source))
    def publish(self,path,value,check=True):
        """Account actual entry growth before accepting durable no-overwrite metadata."""
        if check:self.check()
        self.reserve(value)
        temp=path.with_name('.'+base.uuid.uuid4().hex+'.part');published=False;birth=None
        try:
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            raw=base.canonical(value)+b'\n'
            with os.fdopen(fd,'wb') as stream:
                written=stream.write(raw);need(written==len(raw),'short_promotion_metadata_write')
                stream.flush();os.fsync(stream.fileno())
                info=os.fstat(stream.fileno());birth=(info.st_dev,info.st_ino)
            self.track(temp);self.track(path.parent);self.budget()
            if check:self.check()
            rename=base.ctypes.CDLL(None,use_errno=True).renameat2
            rename.argtypes=[base.ctypes.c_int,base.ctypes.c_char_p,base.ctypes.c_int,base.ctypes.c_char_p,base.ctypes.c_uint]
            rename.restype=base.ctypes.c_int
            if rename(-100,os.fsencode(temp),-100,os.fsencode(path),1)!=0:
                code=base.ctypes.get_errno();raise OSError(code,os.strerror(code))
            published=True;self.track(temp,removed=True);self.track(path);self.track(path.parent);self.budget()
            row={'path':path.name,'bytes':len(raw),'allocated_bytes':info.st_blocks*512,
                 'sha256':hashlib.sha256(raw).hexdigest(),'mode':oct(stat.S_IMODE(info.st_mode))}
            self.sync(path.parent)
            return row
        except BaseException as error:
            try:
                if published:
                    info=path.lstat();need((info.st_dev,info.st_ino)==birth,'record_cleanup_identity')
                    path.unlink();self.track(path,removed=True)
                temp.unlink(missing_ok=True);self.track(temp,removed=True);self.track(path.parent);self.sync(path.parent)
            except BaseException as cleanup:error.add_note('Record cleanup failed: '+type(cleanup).__name__)
            raise
    def record(self,name,value):
        need(name in ('failure.json','outcome.json','outcome-intent.json','terminal.json'),'promotion_record')
        row=self.publish(self.destination/name,value,check=name!='failure.json')
        self.rows.append(row)
        return value
    def finalize(self,complete):
        self.check();self.measure();self.budget()
        need(self.manifest is None,'promotion_manifest_exists')
        value=self.receipt(complete);path=self.destination/'manifest.json'
        row=self.publish(path,value)
        self.manifest={'path':str(path),'sha256':row['sha256']}
        previous=self.complete;self.complete=complete
        try:self.recover();self.check();self.sync(self.destination)
        except BaseException:
            self.complete=previous;raise
    def receipt(self,complete=None):
        if complete is None:complete=self.complete
        return dict(files=self.rows,logical_bytes=self.logical,allocated_bytes=self.allocated,inodes=self.inodes,
                    peak_logical_bytes=self.peak_logical,peak_allocated_bytes=self.peak_allocated,peak_inodes=self.peak_inodes,
                    accounting='Includes partial files and metadata; scratch+both promotions coexist until durable terminal.',
                    manifest=self.manifest,partial=self.partial,scratch_at_promotion=self.scratch_usage,complete=complete)



class AnchoredChild:
    """Retain an unreaped process leader while its group is signalled under one lock."""
    def __init__(self, proc, on_reap=None):
        self.proc = proc
        self.lock = threading.RLock()
        self.on_reap = on_reap
        self.reaped = False
        self.identity = None
        try:
            if base is not None:
                self.identity = base.process(proc.pid)
        except BaseException as error:
            try:
                self.finish()
            except BaseException as cleanup:
                error.add_note('Birth sampling cleanup failed: ' + type(cleanup).__name__)
            finally:
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
            raise

    @property
    def pid(self):
        return self.proc.pid

    @property
    def returncode(self):
        return self.proc.returncode

    def exited(self):
        with self.lock:
            if self.reaped or self.proc.returncode is not None:
                return True
            return os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None

    def finish(self):
        with self.lock:
            if not self.reaped:
                if self.proc.returncode is None:
                    os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if self.identity is not None:
                        need(base.process(self.pid) == self.identity, 'child_birth_identity_changed')
                    try:
                        os.killpg(self.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.proc.wait(timeout=10)
                self.reaped = True
                if self.on_reap is not None:
                    self.on_reap()
            return self.proc.returncode

    def wait(self, timeout):
        end = time.monotonic() + timeout
        while True:
            if self.exited():
                return self.finish()
            if time.monotonic() >= end:
                raise subprocess.TimeoutExpired(self.proc.args, timeout)
            time.sleep(.01)


class SessionGuard:
    """Reusable parent/worker/payload identity checks around project validation."""
    def __init__(self, worker, payload, parent_pid, *, process, refuse):
        self.worker = worker
        self.payload = payload
        self.parent_pid = parent_pid
        self.process = process
        self.refuse = refuse

    def verify(self, code='live_payload_custody'):
        self.refuse(self.process(self.worker['pid']) == self.worker
                    and self.process(os.getpid()) == self.payload
                    and os.getppid() == self.parent_pid, code)

