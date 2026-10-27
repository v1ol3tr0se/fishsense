"""rank: orders the real history into the per-shell shadow history that feeds fish's autosuggestion."""
import functools
import os
import time
from collections import Counter
from math import log1p
from pathlib import Path

import common

W_CWD = 2.0
W_SUB = 0.5
W_TRANS = 14.0
W_FREQ = 1.0
W_FAIL = 4.0
W_REC = 0.5
HALF_LIFE = 14 * 86400
LOG_MAX = 20000
LOG_SLACK = 2000
GRACE = 120


@functools.cache
def _head(cmd):
    """`git commit -m x` -> `git commit`, `ls -la` -> `ls`: commit messages differ, the step doesn't."""
    t = cmd.split(None, 2)
    if len(t) > 1 and not t[1].startswith('-'):
        return f'{t[0]} {t[1]}'
    return t[0] if t else ''


def _word(cmd):
    t = cmd.split(None, 1)
    return t[0] if t else ''


def _transitions(recs, last):
    """head(next) -> P(head(next) | last), from consecutive commands of one shell (pid).
    Falls back from the exact previous command to its head, then to its first word."""
    if not last.strip():
        return {}
    keys = (last, _head(last), _word(last))
    counts = (Counter(), Counter(), Counter())
    prev = {}
    for r in recs:
        p = prev.get(r.pid)
        prev[r.pid] = r.cmd
        if p is not None:
            for c, f, k in zip(counts, (str, _head, _word), keys):
                if f(p) == k:
                    c[_head(r.cmd)] += 1
    for c, factor in zip(counts, (1.0, 0.75, 0.5)):
        if c:
            n = sum(c.values()) + 1
            return {h: factor * v / n for h, v in c.items()}
    return {}


def ranked(items, recs, cwd, last, now):
    """Unique real-history commands as (cmd, paths), ascending by score: fish suggests the last match."""
    uniq = {}
    for it in reversed(items):
        u = uniq.get(it.cmd)
        if u is None:
            uniq[it.cmd] = [it.when, it.paths]
        elif len(it.paths) > len(u[1]):
            u[1] = it.paths

    st, root, rel = {}, cwd.rstrip('/') + '/', {}
    for r in recs:
        w = 0.5 ** (max(0, now - r.when) / HALF_LIFE)
        k = rel.get(r.cwd)
        if k is None:
            d = r.cwd.rstrip('/') + '/'
            k = rel[r.cwd] = 1 if d == root else 2 if d.startswith(root) or root.startswith(d) else 0
        s = st.get(r.cmd)
        if s is None:
            s = st[r.cmd] = [0.0, 0, 0, 0.0, 0.0]
        s[0] += w
        s[1] += 1
        if 0 < r.status < 128:
            s[2] += 1
        if k:
            s[2 + k] += w
    ctx = {c: W_CWD * log1p(s[3]) + W_SUB * log1p(s[4]) + W_FREQ * log1p(s[0]) - W_FAIL * s[2] / s[1]
           for c, s in st.items()}

    trans = _transitions(recs, last)
    n = max(len(uniq) - 1, 1)
    scored = []
    for i, (cmd, (when, paths)) in enumerate(reversed(uniq.items())):
        if not cmd.strip() or cmd.startswith(' '):
            continue
        s = ctx.get(cmd)
        if s is None:
            s = W_FREQ * log1p(0.5 ** (max(0, now - when) / HALF_LIFE))
        if trans:
            s += W_TRANS * trans.get(_head(cmd), 0)
        scored.append((s + W_REC * i / n, i, cmd, paths))
    scored.sort()
    return [(cmd, paths) for _, _, cmd, paths in scored]


def _sessions(fish_data, real_name):
    """Commands of every other real history session (`fish_history=work`); None when any session file,
    this one included, can't be read: pruning against a part of the history would wipe the rest."""
    out = set()
    for p in Path(fish_data).glob('*_history') if fish_data else ():
        if p.name.startswith('fishsense_'):
            continue
        if not os.access(p, os.R_OK):
            return None
        if p.name != f'{real_name}_history':
            out.update(i.cmd for i in common.read_fish_history(p))
    return out


def load_log(items, now, fish_data=None, real_name='fish'):
    """Context log minus records whose command is in no real history session any more, compacted to
    LOG_MAX. The log is shared by every session, so pruning looks at all of them, not only this one.
    Rewritten (atomically) only when that changed something."""
    path = common.log_path()
    try:
        size = path.stat().st_size
    except OSError:
        return []
    recs = common.read_log(path)
    real = {i.cmd for i in items}
    cut = now - GRACE if items else now
    if any(r.cmd not in real and r.when <= cut for r in recs):
        other = _sessions(fish_data, real_name)
        if other is None:
            return recs
        real |= other
    kept = [r for r in recs if not r.cmd.startswith(' ') and (r.cmd in real or r.when > cut)]
    if len(kept) > LOG_MAX + LOG_SLACK:
        kept = kept[-LOG_MAX:]
    if len(kept) != len(recs):
        try:
            common.write_log(kept, path, ok=lambda: path.stat().st_size == size)
        except OSError:
            pass
    return kept


def _static(real):
    try:
        import params
        names = set()
        for d in os.environ.get('PATH', '').split(':'):
            try:
                names.update(os.listdir(d or '.'))
            except OSError:
                pass
        items = map(common.sanitize, params.static_items(names))
        return [(s, ()) for s in items if s.strip() and s not in real]
    except Exception:
        return []


def _args(argv):
    """--key value pairs. Values are url-encoded so they never start with `--`; one that fish's
    command substitution expanded to nothing just leaves its key out instead of eating the next."""
    a = {k[2:].replace('-', '_'): v for k, v in zip(argv, argv[1:] + [''])
         if k.startswith('--') and not v.startswith('--')}
    a['notify'] = '--notify' in argv
    return a


def main(sub, argv):
    os.umask(0o077)
    a = _args(argv)
    pid, fish_data, now = int(a['pid']), a['fish_data'], time.time()
    stamp = common.run_dir() / f'{pid}.rank'

    def stale():
        """fish numbers its requests: a newer one is on its way, don't undo it with stale context.
        Asked between the costly steps, so a burst of cd's doesn't leave a dozen full ranks running."""
        if not a.get('seq'):
            return False
        try:
            return stamp.read_text().strip() != a['seq']
        except OSError:
            return True

    if a.get('log_cmd'):
        try:
            with open(common.log_path(), 'a', encoding='utf-8') as f:
                f.write(f'{int(now)}\t{pid}\t{int(a.get("status") or 0)}\t{a.get("log_cwd", "")}\t{a["log_cmd"]}\n')
        except (OSError, ValueError):
            pass
    if stale():
        return 0
    real_name = a.get('real_name') or 'fish'
    items = common.read_fish_history(common.real_history_path(fish_data, real_name))
    if stale():
        return 0
    recs = load_log(items, now, fish_data, real_name)
    real = ranked(items, recs, common.dec(a.get('cwd', '')) or os.getcwd(),
                  common.dec(a.get('last', '')), now)
    entries = _static({c for c, _ in real}) + real
    if stale():
        return 0
    common.write_atomic(common.shadow_history_path(fish_data, pid), common.format_fish_history(entries))
    if a['notify']:
        common.notify(pid, 'ranked', 0)
    return 0


def doctor_lines():
    recs = common.read_log()
    if not recs:
        return ['ranking: the context log is empty (it fills up as you use the shell)']
    days = (time.time() - recs[0].when) / 86400
    return [f'ranking: {len(recs)} entries in the context log (last {days:.0f} days)']
