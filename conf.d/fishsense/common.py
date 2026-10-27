"""Shared helpers: paths, url codec, sanitizing, fish history and context-log readers."""
import functools
import os
import re
import signal
import time
import urllib.parse
from pathlib import Path
from typing import NamedTuple


def _xdg(var, default):
    v = os.environ.get(var)
    return Path(v) if v else Path.home() / default


def data_dir():
    return _xdg('XDG_DATA_HOME', '.local/share') / 'fishsense'


def cache_dir():
    return _xdg('XDG_CACHE_HOME', '.cache') / 'fishsense'


def run_dir():
    r = os.environ.get('XDG_RUNTIME_DIR')
    d = Path(r) / 'fishsense' if r else cache_dir() / 'run'
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def log_path():
    return data_dir() / 'log.tsv'


def enc(s):
    return urllib.parse.quote(s, safe='', errors='surrogateescape')


def dec(s):
    return urllib.parse.unquote_to_bytes(s).decode('utf-8', 'surrogateescape')


_CTRL = re.compile('[\x00-\x1f\x7f-\x9f]')
_CTRL_KEEP_NL = re.compile('[\x00-\x09\x0b-\x1f\x7f-\x9f]')


def sanitize(s, keep_newlines=False):
    """Drop C0/C1 control characters (escape sequences can't reach the terminal or fish code)."""
    return (_CTRL_KEEP_NL if keep_newlines else _CTRL).sub('', s)


def write_atomic(path, text, ok=None):
    """tmp + rename. ok() is asked right before the rename: False leaves path as it is.
    The tmp never outlives the call (a full disk must not leave a copy of the history behind)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(text, encoding='utf-8', errors='surrogateescape')
        if ok is None or ok():
            os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class HistItem(NamedTuple):
    cmd: str
    when: int
    paths: tuple


def unescape_hist(s):
    if '\\' not in s:
        return s
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s) and s[i + 1] in '\\n':
            out.append('\\' if s[i + 1] == '\\' else '\n')
            i += 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def escape_hist(s):
    return s.replace('\\', '\\\\').replace('\n', '\\n')


def read_fish_history(path):
    """Items in file order (oldest first). Missing file -> []."""
    try:
        lines = Path(path).read_bytes().decode('utf-8', 'surrogateescape').split('\n')
    except OSError:
        return []
    items, cmd, when, paths, in_paths = [], None, 0, [], False

    def flush():
        if cmd is not None:
            items.append(HistItem(cmd, when, tuple(paths)))

    for line in lines:
        if line.startswith('- cmd: '):
            flush()
            cmd, when, paths, in_paths = unescape_hist(line[7:]), 0, [], False
        elif cmd is None:
            continue
        elif line.startswith('  when: '):
            try:
                when = int(line[8:])
            except ValueError:
                when = 0
            in_paths = False
        elif line.startswith('  paths:'):
            in_paths = True
        elif in_paths and line.startswith('    - '):
            paths.append(unescape_hist(line[6:]))
    flush()
    return items


def format_fish_history(entries, when=None):
    """entries: iterable of (cmd, paths). Returns file text, entries kept in the given order."""
    when = int(time.time()) - 5 if when is None else when
    out = []
    for cmd, paths in entries:
        out.append(f'- cmd: {escape_hist(cmd)}\n  when: {when}\n')
        if paths:
            out.append('  paths:\n')
            out.extend(f'    - {escape_hist(p)}\n' for p in paths)
    return ''.join(out)


def real_history_path(fish_data, real_name):
    return Path(fish_data) / f'{real_name}_history'


def shadow_history_path(fish_data, pid):
    return Path(fish_data) / f'fishsense_{pid}_history'


class LogRec(NamedTuple):
    when: int
    pid: int
    status: int
    cwd: str
    cmd: str


def read_log(path=None):
    try:
        text = Path(path or log_path()).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    recs, cwd = [], functools.cache(dec)
    for line in text.splitlines():
        f = line.split('\t')
        if len(f) != 5:
            continue
        try:
            recs.append(LogRec(int(f[0]), int(f[1]), int(f[2]), cwd(f[3]), dec(f[4])))
        except ValueError:
            continue
    return recs


def write_log(recs, path=None, ok=None):
    write_atomic(path or log_path(), ''.join(
        f'{r.when}\t{r.pid}\t{r.status}\t{enc(r.cwd)}\t{enc(r.cmd)}\n' for r in recs), ok)


def notify(pid, kind, req_id, payload=''):
    stamp, res = run_dir() / f'{pid}.rank', run_dir() / f'{pid}.result'
    if not stamp.exists():
        return
    write_atomic(res, f'{kind}\t{req_id}\t{enc(payload)}\n')
    try:
        os.kill(int(pid), signal.SIGUSR1)
        for _ in range(50):
            time.sleep(0.02)
            os.kill(int(pid), 0)
            if not res.stat().st_size:
                break
        if stamp.exists():
            os.kill(int(pid), signal.SIGUSR1)
    except (OSError, ValueError):
        pass


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def gc_shadows(fish_data):
    """Remove shadow histories left behind by shells that died without fish_exit."""
    for p in Path(fish_data).glob('fishsense_*_history'):
        pid = p.name[len('fishsense_'):-len('_history')]
        if pid.isdigit() and not pid_alive(pid):
            p.unlink(missing_ok=True)


def path_executables():
    """name -> full path for every executable on $PATH (first one wins, like the shell)."""
    found = {}
    for d in dict.fromkeys(os.environ.get('PATH', '').split(':')):
        try:
            with os.scandir(d or '.') as it:
                for e in it:
                    if e.name not in found and e.is_file() and os.access(e.path, os.X_OK):
                        found[e.name] = e.path
        except OSError:
            continue
    return found
