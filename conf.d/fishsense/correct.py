"""`correct`: fix a mistyped command word or subcommand before Enter runs the line.

stdin: names one per line (`name` or `name<TAB>description`). Lines under `#cmds` are builtins, lines
under `#funcs` functions (aliases included); lines under `#sub <cmd>` are subcommand candidates for <cmd>
(the command word after sudo/env/... prefixes, i.e. `complete -C '<cmd> '` minus file completions), which
fish only sends for a command it knows; a `#sub` description starting with `alias` marks one of the command's
own aliases, whose fix asks like a function's.
Prints `fix\t<url(line)>`, `ask\t<url(line)>` or nothing.
"""

import functools
import os
import re
import shutil
import sys

import common

SUDO = {'sudo', 'doas', 'run0', 'pkexec'}
FISH_PREFIX = {'command', 'builtin', 'exec', 'time'}
PREFIX = SUDO | FISH_PREFIX | {'env'}
VALUE_FLAGS = {'-u', '-g', '-C', '-D', '-U', '-p', '-r', '-t', '-T', '-R', '--user', '--group', '--chdir'}
DESTRUCTIVE = {
    'rm',
    'rmdir',
    'dd',
    'shred',
    'wipefs',
    'fdisk',
    'sfdisk',
    'parted',
    'sgdisk',
    'mv',
    'cp',
    'chmod',
    'chown',
    'kill',
    'killall',
    'pkill',
    'shutdown',
    'reboot',
    'poweroff',
    'halt',
    'systemctl',
}
GIT_DESTRUCTIVE = {'push', 'reset', 'clean', 'rebase', 'checkout', 'restore'}
NAME = re.compile(r'[A-Za-z0-9_][\w.+,:@-]*')
SUB = re.compile(r'[A-Za-z][\w-]*')
STOP = ';|&<>\n'


def _words(line):
    """(start, end, value) per word of the first process. A word that expands ($var, (cmd), globs,
    braces, ~, escapes like \\n, unterminated quotes) ends the list with value None."""
    out, i, n = [], 0, len(line)
    while True:
        while i < n and line[i] in ' \t':
            i += 1
        if i >= n or line[i] in STOP or line[i] == '#':
            return out
        start, val, q, bad = i, [], '', False
        while i < n and (q or line[i] not in ' \t' + STOP):
            c, nxt = line[i], line[i + 1 : i + 2]
            if c == '\\':
                esc = {"'": "'\\", '"': '"\\$'}.get(q)
                if esc is None:
                    if not nxt or nxt.isalnum() or nxt == '\n':
                        bad = True
                        break
                    val.append(nxt)
                    i += 2
                elif nxt and nxt in esc:
                    val.append(nxt)
                    i += 2
                elif q == '"' and nxt == '\n':
                    bad = True
                    break
                else:
                    val.append(c)
                    i += 1
                continue
            if q and c == q:
                q = ''
            elif not q and c in '\'"':
                q = c
            elif (q == '"' and c == '$') or (not q and (c in '$()*?{}' or (c == '~' and i == start))):
                bad = True
                break
            else:
                val.append(c)
            i += 1
        if bad or q:
            out.append((start, i, None))
            return out
        out.append((start, i, ''.join(val)))


def _dl(a, b, cap=None):
    """Damerau-Levenshtein distance (optimal string alignment). With cap, anything farther comes back as
    cap + 1 as soon as a whole row is past it (thousands of PATH names, almost all far away)."""
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if cap is not None and min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


def _best(word, cands, usage, used_only=False):
    """The single closest candidate, or the clear favourite (>=3x uses) among equally close ones.
    Up to 1 typo for 3 letters, 2 for 6, 3 past that; with used_only, a fix needing more than 1 typo
    (4 letters) / 2 (longer) only goes to something already run, or uninstalled tool names land on
    random PATH binaries (neomutt -> permute)."""
    n, ws = len(word), set(word)
    cap = 1 if n <= 3 else 2 if n <= 6 else 3
    scored = [(d, c) for c in cands if abs(len(c) - n) <= cap and len(ws ^ set(c)) <= 2 * cap and (d := _dl(word, c, cap)) <= cap]
    if not scored:
        return None
    m = min(d for d, _ in scored)
    top = [c for d, c in scored if d == m]
    if used_only and m > (1 if n <= 4 else 2):
        uses = usage(set(top))
        top = [c for c in top if uses.get(c, 0)]
        if not top:
            return None
    if len(top) == 1:
        return top[0]
    uses = usage(set(top))
    top.sort(key=lambda c: -uses.get(c, 0))
    a, b = uses.get(top[0], 0), uses.get(top[1], 0)
    return top[0] if a > 0 and a >= 3 * b else None


def destructive(line):
    for seg in re.split(r'[;|&\n()]', line):
        w = [os.path.basename(re.sub(r'[\'"\\]', '', x)) for x in seg.split()]
        if any(x in DESTRUCTIVE or x in SUDO or x.startswith('mkfs') for x in w):
            return True
        if 'git' in w:
            rest = w[w.index('git') + 1 :]
            short = ''.join(x[1:] for x in rest if re.fullmatch(r'-[A-Za-z]+', x))
            force = 'D' in short or ('d' in short or '--delete' in rest) and ('f' in short or '--force' in rest)
            if GIT_DESTRUCTIVE & set(rest) or ('branch' in rest and force):
                return True
    return False


def _node_bin(cwd, w):
    """yarn/pnpm/npx run node_modules/.bin/<w> of this package or any above it by name."""
    d = os.path.abspath(cwd)
    while True:
        if os.path.lexists(os.path.join(d, 'node_modules', '.bin', w)):
            return True
        if d == (d := os.path.dirname(d)):
            return False


def correct_line(line, cwd, exists, cands, subs, usage, wrapper=lambda w: False, aliases={}):
    """-> (kind, line) or None.
    exists(name) -> bool; cands() -> command-word candidates; subs: {cmd: set(subcommands)};
    usage(names, cmd) -> {name: uses}, cmd None when counting command words;
    wrapper(name) -> True to ask before a fix to that function/alias (now always False: function/alias
    fixes just apply; only a visibly destructive command still asks via destructive());
    aliases: {cmd: set(subcommands that are its own aliases)}, whose fix still asks (git wipe = reset --hard)."""
    if not line or line[0].isspace():
        return None
    words, edits, cmd, i, risky = _words(line), [], None, 0, False
    while i < len(words):
        s, e, w = words[i]
        if w is None or '/' in w or '=' in w:
            return None
        if w not in FISH_PREFIX and not exists(w):
            if len(w) < 2 or w.isdigit() or not NAME.fullmatch(w) or os.path.lexists(os.path.join(cwd, w)):
                return None
            w = _best(w, cands(), lambda names: usage(names, None), used_only=True)
            if not w:
                return None
            edits.append((s, e, w))
            risky = risky or wrapper(w)
        i += 1
        if w not in PREFIX:
            cmd = w
            break
        while i < len(words):
            v = words[i][2]
            if v is None:
                return None
            if w not in FISH_PREFIX and '=' in v and not v.startswith('-'):
                i += 1
            elif v.startswith('-'):
                if w not in SUDO:
                    return None
                i += 2 if v in VALUE_FLAGS else 1
                if v == '--':
                    break
            else:
                break

    if cmd in subs and i < len(words):
        s, e, w = words[i]
        names = {c for c in subs[cmd] if SUB.fullmatch(c)}
        if (
            w
            and len(w) > 1
            and not any(c.startswith(w) for c in subs[cmd])
            and SUB.fullmatch(w)
            and not os.path.lexists(os.path.join(cwd, w))
            and not _node_bin(cwd, w)
        ):
            fixed = _best(w, names, lambda n: usage(n, cmd))
            if fixed and not os.path.lexists(os.path.join(cwd, fixed)):
                edits.append((s, e, fixed))
                risky = risky or fixed in aliases.get(cmd, ())

    if not edits:
        return None
    for s, e, r in reversed(edits):
        line = line[:s] + r + line[e:]
    return ('ask' if risky or destructive(line) else 'fix'), line


def _head(cmd):
    """(command word, first argument) of a history line, skipping prefixes roughly."""
    w = cmd.split()
    i = 0
    while i < len(w) and (w[i] in PREFIX or '=' in w[i] or w[i].startswith('-')):
        i += 1
    return (w[i] if i < len(w) else ''), (w[i + 1] if i + 1 < len(w) else '')


@functools.cache
def _ran(fish_data, real_name):
    """The user's command lines: the real history, and every run in the log still url-escaped (decoding 20k
    records is most of the cost, and only the few that mention a candidate need it)."""
    hist = [h.cmd for h in common.read_fish_history(common.real_history_path(fish_data, real_name))]
    try:
        log = [line.rpartition('\t')[2] for line in common.log_path().read_text(encoding='utf-8', errors='replace').splitlines()]
    except OSError:
        log = []
    return hist, log


def _usage(fish_data, real_name, names, cmd):
    """{name: uses} as a command word (cmd None) or as cmd's first argument."""
    hist, log = _ran(fish_data, real_name)
    keys = [common.enc(x) for x in names]
    n = {}
    for line in [h for h in hist if any(x in h for x in names)] + [common.dec(r) for r in log if any(k in r for k in keys)]:
        c, a = _head(line)
        k = c if cmd is None else (a if c == cmd else None)
        if k in names:
            n[k] = n.get(k, 0) + 1
    return n


def main(sub, argv):
    o = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith('--')}
    fish_data = o.get('--fish-data') or str(common.data_dir().parent / 'fish')
    real_name = o.get('--real-name') or 'fish'
    cmds, funcs, subs, aliases, cur, sub = set(), set(), {}, {}, None, None
    text = '' if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read().decode('utf-8', 'replace')
    for raw in text.splitlines():
        name, _, desc = raw.partition('\t')
        name = name.strip()
        if name == '#cmds':
            cur, sub = cmds, None
        elif name == '#funcs':
            cur, sub = funcs, None
        elif name.startswith('#sub '):
            sub = name[5:].strip()
            cur = subs.setdefault(sub, set())
        elif name:
            (cmds if cur is None else cur).add(name)
            if sub and desc.lower().startswith('alias'):
                aliases.setdefault(sub, set()).add(name)

    @functools.cache
    def cands():
        return {c for c in cmds | funcs | common.path_executables().keys() if NAME.fullmatch(c) and not c.startswith('_')}

    r = correct_line(
        common.dec(o.get('--line', '')),
        common.dec(o.get('--cwd', '')),
        lambda w: w in cmds or w in funcs or w in subs or shutil.which(w) is not None,
        cands,
        subs,
        lambda names, cmd: _usage(fish_data, real_name, names, cmd),
        lambda w: False,
        aliases,
    )
    if r:
        print(f'{r[0]}\t{common.enc(r[1])}')
    return 0
