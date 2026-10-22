"""Bounded metadata inventory and explicit, fail-closed owned terminal retention.

No discovery grants deletion authority. Callbacks must bind canonical runner job,
invocation and process birth to quiescent terminal proof and current references.
Only registered leaves / empty directories are removed; no recursive deletion.
"""
import copy
import os
from pathlib import Path
import stat
import time

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELLED'}


def identity(info):
    return {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _absolute(path):
    path = Path(path)
    _require(path.is_absolute() and '..' not in path.parts and path != Path('/'), 'invalid owned root')
    return path


def _open_root(path):
    """Anchor every component at a held parent; reject symlink ancestors."""
    path = _absolute(path)
    fd = os.open('/', DIR_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        _require(os.fstat(fd).st_uid == os.getuid(), 'foreign root UID')
        return fd
    except BaseException:
        os.close(fd)
        raise


def inventory(root, max_entries=5000, deadline=None):
    """deadline is an absolute time.monotonic(); budget always <=2s/5000 rows.

    Results are non-atomic observations, never ownership or reference proof.
    Allocation deduplicates observed inodes; hardlinks can retain all data blocks.
    """
    root = _absolute(root)
    end = min(time.monotonic() + 2, deadline if deadline is not None else float('inf'))
    limit = min(5000, max(0, int(max_entries)))
    result = {'root': str(root), 'rows': [], 'gaps': [], 'complete': False,
              'allocated_bytes_observed': 0, 'reference_state': 'UNKNOWN',
              'max_entries': limit, 'max_seconds': 2, 'contents_read': False}
    seen = set()
    started = time.monotonic()
    fd = None

    def bounded():
        if len(result['rows']) >= limit:
            raise ValueError('entry cap reached')
        if time.monotonic() >= end:
            raise ValueError('deadline reached')

    def walk(parent, relative, depth):
        _require(depth <= 64, 'depth cap reached')
        with os.scandir(parent) as entries:
            for entry in entries:
                bounded()
                name = entry.name
                child = str(Path(relative) / name)
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                kind = ('symlink' if stat.S_ISLNK(info.st_mode) else
                        'directory' if stat.S_ISDIR(info.st_mode) else
                        'file' if stat.S_ISREG(info.st_mode) else 'special')
                key = (info.st_dev, info.st_ino)
                allocated = info.st_blocks * 512 if key not in seen else 0
                seen.add(key)
                result['allocated_bytes_observed'] += allocated
                result['rows'].append({'path': child, 'identity': identity(info), 'kind': kind,
                                       'size': info.st_size, 'allocated_bytes': allocated,
                                       'nlink': info.st_nlink, 'references': 'UNKNOWN'})
                if kind != 'directory':
                    continue
                if info.st_uid != os.getuid() or info.st_dev != result['root_identity']['device']:
                    result['gaps'].append({'path': child, 'reason': 'foreign UID/device; not traversed'})
                    continue
                subfd = os.open(name, DIR_FLAGS, dir_fd=parent)
                try:
                    _require(identity(os.fstat(subfd)) == identity(info), 'directory replaced')
                    walk(subfd, child, depth + 1)
                finally:
                    os.close(subfd)
    try:
        fd = _open_root(root)
        result['root_identity'] = identity(os.fstat(fd))
        walk(fd, '.', 0)
        check = _open_root(root)
        try:
            _require(identity(os.fstat(check)) == result['root_identity'], 'root replaced')
        finally:
            os.close(check)
        result['complete'] = not result['gaps']
    except (OSError, ValueError) as error:
        result['gaps'].append({'reason': str(error)[:240], 'type': type(error).__name__})
    finally:
        if fd is not None:
            os.close(fd)
    result['elapsed_seconds'] = time.monotonic() - started
    result['truncated'] = not result['complete']
    return result


def _relative(path):
    path = Path(path)
    _require(not path.is_absolute() and path.parts and all(p not in ('..', '.') for p in path.parts), 'invalid relative path')
    return path


def _intersects(path, reference):
    a, b = Path(path), Path(reference)
    return a == b or a in b.parents or b in a.parents


def _reference_reason(artifact, root, refs):
    if not isinstance(refs, dict) or refs.get('complete') is not True:
        return 'live references unresolved'
    if not isinstance(refs.get('paths'), list):
        return 'reference paths missing'
    if artifact.get('reference_state') != 'resolved' or artifact.get('refpaths') != []:
        return 'registered references unresolved or retained'
    absolute = root / _relative(artifact['path'])
    for ref in refs['paths']:
        try:
            ref = Path(ref)
        except TypeError:
            return 'invalid reference boundary'
        if not ref.is_absolute() or '..' in ref.parts:
            return 'invalid reference boundary'
        if _intersects(absolute, ref):
            return 'live reference'
        try:
            canonical = ref.resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            return 'live references unresolved'
        if _intersects(absolute, canonical):
            return 'live reference'
    return None


def classify(artifact, root, live_refs):
    """Return explicit retention reason or eligible; missing safety fields retain."""
    if artifact.get('owner') is None or artifact.get('job_id') is None or not artifact.get('job_identity'):
        return 'ownership/job identity missing'
    for field in ('unique', 'source', 'data', 'rollback', 'active'):
        if artifact.get(field) is not False:
            return field + ' protected or unknown'
    if artifact.get('reproducible') is not True or artifact.get('disposable') is not True:
        return 'not explicitly reproducible disposable scratch'
    if artifact.get('usefulness') not in ('terminal-scratch', 'reproducible-output'):
        return 'usefulness unresolved or retained'
    if artifact.get('dependencies') != []:
        return 'dependencies unresolved or retained'
    if artifact.get('family_baseline') is not False:
        if artifact.get('replacement_verified') is not True:
            return 'sole family baseline or unknown replacement'
    if artifact.get('kind') not in ('file', 'directory'):
        return 'symlink/special/unknown kind'
    if artifact['kind'] == 'file' and artifact.get('nlink') != 1:
        return 'hardlinked file'
    if artifact.get('terminal') not in TERMINAL or artifact.get('terminal_verified') is not True:
        return 'terminal proof absent'
    return _reference_reason(artifact, Path(root), live_refs) or 'eligible'


def plan_cleanup(manifest, pressure=False, needed_bytes=0, live_refs=None):
    """Order terminal scratch promptly, large/low-reuse outputs under pressure.

    No count/age policy. Every artifact is explicit; unknown fields fail closed.
    Empty-directory ordering allows registered child leaves first.
    """
    root = _absolute(manifest['root'])
    _require(manifest.get('owner') and manifest.get('root_identity'), 'root ownership missing')
    accepted, retained, seen = [], [], set()
    for original in manifest.get('artifacts', []):
        artifact = copy.deepcopy(original)
        reason = 'eligible'
        try:
            path = str(_relative(artifact['path']))
            _require(path not in seen, 'duplicate artifact')
            seen.add(path)
            _require(artifact.get('owner') == manifest['owner'], 'owner mismatch')
            _require(artifact.get('identity', {}).get('uid') == os.getuid(), 'foreign or unknown artifact UID')
            _require(artifact['identity'].get('device') == manifest['root_identity']['device'], 'foreign device')
            reason = classify(artifact, root, live_refs)
        except (KeyError, ValueError) as error:
            reason = str(error)
        (accepted if reason == 'eligible' else retained).append(artifact if reason == 'eligible' else {'path': artifact.get('path'), 'reason': reason})
    accepted.sort(key=lambda a: (a['kind'] == 'directory',
                                 -len(Path(a['path']).parts) if a['kind'] == 'directory' else 0,
                                 a.get('reuse_score', 0) if pressure else 0,
                                 -a.get('allocated_bytes', 0) if pressure else 0, a['path']))
    # Needed bytes is a target rather than permission; all eligible terminal scratch may go.
    return {'root': str(root), 'root_identity': copy.deepcopy(manifest['root_identity']),
            'owner': manifest['owner'], 'artifacts': accepted, 'retained': retained,
            'pressure': bool(pressure), 'needed_bytes': max(0, needed_bytes)}


def _terminal(artifact, callback):
    proof = callback(copy.deepcopy(artifact))
    _require(isinstance(proof, dict) and proof.get('identity_verified') is True and
             proof.get('state') in TERMINAL and proof.get('quiescent') is True and
             proof.get('job_id') == artifact['job_id'] and
             proof.get('job_identity') == artifact['job_identity'], 'terminal identity/quiescence not verified')


def cleanup(plan, terminal_verified_callback, refs_callback):
    """Callbacks must refresh external state; exceptions refuse the artifact.

    No environment, process or manager mutation. Call under the same exclusive
    owner lock as scratch generation; same-UID concurrent writers cannot be
    excluded by POSIX inode-check/unlink alone.
    """
    root = _absolute(plan['root'])
    receipts = []
    reclaimed = 0
    for artifact in plan['artifacts']:
        receipt = {'path': artifact.get('path'), 'removed': False, 'reclaimed_allocated_bytes': 0}
        rootfd = parentfd = None
        try:
            _require(artifact.get('owner') == plan['owner'], 'owner mismatch')
            path = _relative(artifact['path'])
            rootfd = _open_root(root)
            _require(identity(os.fstat(rootfd)) == plan['root_identity'], 'root replaced')
            _terminal(artifact, terminal_verified_callback)
            _require(classify(artifact, root, refs_callback()) == 'eligible', 'references or classification changed')
            parentfd = os.dup(rootfd)
            parent_births = []
            for part in path.parts[:-1]:
                child = os.open(part, DIR_FLAGS, dir_fd=parentfd)
                os.close(parentfd)
                parentfd = child
                current = os.fstat(parentfd)
                _require(current.st_uid == os.getuid() and current.st_dev == plan['root_identity']['device'], 'foreign ancestor')
                parent_births.append(identity(current))
            before = os.stat(path.name, dir_fd=parentfd, follow_symlinks=False)
            _require(identity(before) == artifact['identity'], 'artifact replaced')
            _require((artifact['kind'] == 'file' and stat.S_ISREG(before.st_mode) and before.st_nlink == 1) or
                     (artifact['kind'] == 'directory' and stat.S_ISDIR(before.st_mode)), 'kind/hardlink changed')
            # Revalidate after all external callbacks, immediately before mutation.
            _terminal(artifact, terminal_verified_callback)
            _require(classify(artifact, root, refs_callback()) == 'eligible', 'references or classification changed')
            check = _open_root(root)
            try:
                _require(identity(os.fstat(check)) == plan['root_identity'], 'root replaced')
                for part, birth in zip(path.parts[:-1], parent_births):
                    child = os.open(part, DIR_FLAGS, dir_fd=check)
                    os.close(check)
                    check = child
                    _require(identity(os.fstat(check)) == birth, 'ancestor replaced')
            finally:
                os.close(check)
            current = os.stat(path.name, dir_fd=parentfd, follow_symlinks=False)
            _require(identity(current) == artifact['identity'] and current.st_mode == before.st_mode and current.st_nlink == before.st_nlink,
                     'artifact changed before cleanup')
            if artifact['kind'] == 'directory':
                os.rmdir(path.name, dir_fd=parentfd)
            else:
                os.unlink(path.name, dir_fd=parentfd)
            allocated = current.st_blocks * 512
            reclaimed += allocated
            receipt.update(removed=True, reclaimed_allocated_bytes=allocated)
        except Exception as error:
            receipt['reason'] = str(error)[:240]
        finally:
            if parentfd is not None:
                os.close(parentfd)
            if rootfd is not None:
                os.close(rootfd)
        receipts.append(receipt)
    return {'receipts': receipts, 'reclaimed_allocated_bytes': reclaimed,
            'accounting': 'unlinked allocated bytes; global free-space delta not attributed'}
