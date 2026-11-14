"""Local background-sync configuration and durable per-library progress.

No tokens are stored here. Only the explicitly configured laptop starts the user service.
"""
import contextlib
import contextvars
import fcntl
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone

import photo_library

_deferred = contextvars.ContextVar('defer_photo_sync', default=False)


def _path(name):
    return photo_library.REGISTRY.parent / name


def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def config():
    return _read(_path('sync-config.json'))


def _edit(name, change):
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix('.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(path)
        change(data)
        tmp = path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    return data


def configure(server, cacert):
    _edit('sync-config.json', lambda d: d.update(enabled=True, server=server, cacert=cacert))


def schedule():
    if config().get('enabled') and not _deferred.get():
        try:
            subprocess.run(['systemctl', '--user', 'start', '--no-block', 'photos-laptop-sync.service'],
                           timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass  # The periodic timer will retry; mutations never fail over scheduling.


def changed(library_id, launch=True):
    if not config().get('enabled'):
        return
    def update(data):
        state = data.setdefault(library_id, {})
        state.update(state='pending', generation=uuid.uuid4().hex, error='')
    _edit('sync-state.json', update)
    if launch:
        schedule()


def begin(library_id):
    def update(data):
        state = data.setdefault(library_id, {})
        state.setdefault('generation', uuid.uuid4().hex)
        state.update(state='syncing', attempted_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), error='')
    return _edit('sync-state.json', update)[library_id]['generation']


def finish(library_id, generation, *, success=False, error='', fingerprint=None):
    def update(data):
        state = data.setdefault(library_id, {})
        newer = state.get('generation') != generation
        state['state'] = 'pending' if newer else 'synced' if success else 'failed'
        state['error'] = '' if newer or success else error
        if success:
            state.update(last_success=datetime.now(timezone.utc).isoformat(timespec='seconds'), fingerprint=fingerprint)
    _edit('sync-state.json', update)


def statuses():
    return _read(_path('sync-state.json'))


def view(lib):
    if not config().get('enabled'):
        return None
    state = statuses().get(lib.id, {'state': 'pending'})
    if not lib.online:
        return {**state, 'state': 'disconnected'}
    # A killed worker must not leave "syncing" displayed indefinitely.
    if state.get('state') == 'syncing' and not running():
        return {**state, 'state': 'pending', 'error': 'Previous sync stopped; automatic retry is pending.'}
    return state


def running():
    path = _path('sync-run.lock')
    if not path.exists():
        return False
    with open(path, 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


@contextlib.contextmanager
def run_lock():
    path = _path('sync-run.lock'); path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


@contextlib.contextmanager
def defer():
    token = _deferred.set(True)
    try:
        yield
    finally:
        _deferred.reset(token)
