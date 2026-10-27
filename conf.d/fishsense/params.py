"""Parameters and structure: help/man/source parsing, generated completions, tldr pages, next-param ghost."""

import json
import os
import re
import time
from pathlib import Path

import common

LANGS = ('en',)
PLATFORMS = ('linux', 'common')
TLDR_URL = 'https://github.com/tldr-pages/tldr/releases/latest/download/tldr-pages.{}.zip'
HELP_TIMEOUT = 2
DENY = {
    'shutdown',
    'reboot',
    'poweroff',
    'halt',
    'rm',
    'dd',
    'systemctl',
    'init',
    'telinit',
    'sudo',
    'doas',
    'su',
    'pkexec',
    'run0',
    'login',
    'passwd',
    'chsh',
    'X',
    'Xorg',
    'Hyprland',
    'startx',
    'systemd',
    'pkill',
    'xkill',
    'shred',
    'wipefs',
    'fdisk',
    'sfdisk',
    'parted',
    'sgdisk',
}
WRAPPERS = {
    'sudo',
    'doas',
    'run0',
    'pkexec',
    'command',
    'builtin',
    'exec',
    'time',
    'nice',
    'nohup',
    'env',
    'and',
    'or',
    'not',
    'begin',
    'if',
    'while',
    'else',
}
SUB_VARS = {'1', 'cmd', 'command', 'action', 'sub', 'subcommand', 'verb', 'op', 'mode'}

_NAME = re.compile(r'[\w+@:,-][\w.+@:,=-]*')
_SAFE_VAL = re.compile(r'[\w.+:,@%=-]+')
_LONG = re.compile(r'[\w?@#][\w.+@#-]*')
_SUBWORD = re.compile(r'[a-z][\w.-]*')
_GENERIC = {'string', 'str', 'strings', 'stringarray', 'value', 'val', 'arg', 'args', 'text', 'x', 's', 'n', 'uint', 'int'}
_FILEISH = re.compile(r'path|file|dir|input|output|folder', re.I)


_ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])')


def _lines(text):
    text = re.sub(r'.\x08', '', _ANSI.sub('', text))
    return [common.sanitize(l.expandtabs(8)).rstrip() for l in text.splitlines()]


def _ind(line):
    return len(line) - len(line.lstrip())


def _split(s):
    """Shell-ish words keeping quotes and tldr {{…}} groups whole; None while a quote is open."""
    words, cur, q, i = [], '', None, 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            cur += s[i : i + 2]
            i += 2
            continue
        if q:
            q = None if c == q else q
        elif s.startswith('{{', i):
            j = s.find('}}', i + 2)
            j = len(s) if j < 0 else j + 2
            cur += s[i:j]
            i = j
            continue
        elif c in '\'"':
            q = c
        elif c.isspace():
            if cur:
                words.append(cur)
            cur, i = '', i + 1
            continue
        cur += c
        i += 1
    if q:
        return None
    return words + [cur] if cur else words


def _segments(s):
    """Word lists of every simple command in s (split on ; | & newline and parens), wrappers skipped."""
    pieces, cur, q = [], '', None
    for i, c in enumerate(s):
        if q:
            q = None if c == q and s[i - 1] != '\\' else q
        elif c in '\'"':
            q = c
        elif c in ';|&\n()':
            pieces.append(cur)
            cur = ''
            continue
        cur += c
    pieces.append(cur)
    out = []
    for p in pieces:
        w = _split(p)
        if w is None:
            out.append(None)
            continue
        while w and (w[0] in WRAPPERS or re.fullmatch(r'\w+=\S*', w[0])):
            w = w[1:]
        if w and w[0].startswith('-'):
            w = []
        if w and '/' in w[0]:
            w[0] = os.path.basename(w[0])
        out.append(w)
    return out


def next_of(it):
    """First item or None (the module's own `next` shadows the builtin)."""
    for x in it:
        return x
    return None


def _isflag(w):
    return len(w) > 1 and w.startswith('-') and w != '--'


def _flag(w):
    return w.split('=', 1)[0]


def _q(s):
    return "'" + s.replace('\\', '\\\\').replace("'", "\\'") + "'"


def _clean_desc(d, limit=100):
    d = re.sub(r'\s*\[(?:env|possible values): [^\]]*\]', '', re.sub(r'\s+', ' ', common.sanitize(d))).strip()
    return d if len(d) <= limit else d[:limit].rsplit(' ', 1)[0] + '…'


_OPTW = re.compile(r'(--?)(\[no-?\])?([A-Za-z0-9?.@#][\w.+@#-]*?)(\.\.\.)?(\[?=.*|\[.*|<.*)?')
_ARGW = re.compile(r'(<[^>]*>|\[[^\]]*\]|\{[^}]*\}|[A-Za-z#"][^\s]*)(\.\.\.)?')
_OPTSTART = re.compile(r'--?[\w?.@#\[]')


def _new_opt():
    return {'s': [], 'l': [], 'o': [], 'arg': None, 'opt': False, 'd': ''}


def _optparts(w):
    parts = [p for p in re.split(r'[,|]', w) if p]
    ms = [_OPTW.fullmatch(p) for p in parts] if w.startswith('-') else []
    return ms if ms and all(ms) else None


def _add_name(o, m):
    dash, neg, name, _, att = m.groups()
    if dash == '--':
        o['l'] += [name] + ([f'no-{name}'] if neg else [])
    elif len(name) == 1:
        o['s'].append(name)
    else:
        o['o'].append(name)
    if att:
        o['opt'] = att.startswith('[')
        o['arg'] = re.sub(r'[\[\]<>=]', '', att) or o['arg']


def _opt_line(s):
    """'-o, --output=FILE  desc' -> option dict (desc on the same line, if any) or None."""
    toks = list(re.finditer(r'\S+', s))
    o, i, want_arg = _new_opt(), 0, False
    while i < len(toks):
        w = toks[i].group()
        ms = _optparts(w.rstrip(','))
        if ms:
            for m in ms:
                _add_name(o, m)
            want_arg = True
        elif w in ('|', 'or', '/'):
            pass
        elif want_arg and _ARGW.fullmatch(w.rstrip(',')) and not re.fullmatch(r'[A-Z][a-z]+', w):
            if w.startswith('"') and not (len(w) > 1 and w.endswith('"')):
                start = toks[i].start()
                while i + 1 < len(toks) and not toks[i].group().endswith('"'):
                    i += 1
                w = s[start : toks[i].end()]
            if o['arg'] is None:
                o['arg'] = w.rstrip(',').strip('"')
            want_arg = False
        else:
            break
        end, i = toks[i].end(), i + 1
        if i < len(toks) and toks[i].start() - end >= 2 and not toks[i - 1].group().endswith(','):
            if not _optparts(toks[i].group().rstrip(',')):
                break
    if not (o['s'] or o['l'] or o['o']):
        return None
    o['d'] = s[toks[i].start() :] if i < len(toks) else ''
    o['col'] = toks[i].start() if i < len(toks) else None
    return o


def _scan_opts(lines):
    """Option entries; descriptions may sit on the same line or on deeper-indented lines (man style)."""
    opts, cur, cur_ind, desc_ind, done = [], None, 0, 1 << 30, False
    for l in lines:
        s = l.strip()
        if not s:
            done = cur is not None and bool(cur['d'])
            continue
        ind = _ind(l)
        if cur and ind > cur_ind and (ind >= desc_ind or not _OPTSTART.match(s)):
            desc_ind = min(desc_ind, ind)
            if not done:
                cur['d'] += ' ' + s
            continue
        o = _opt_line(s) if _OPTSTART.match(s) else None
        cur, done = o, False
        if o:
            col = o.pop('col')
            cur_ind, desc_ind = ind, ind + col if col is not None else 1 << 30
            opts.append(o)
    return opts


_VALRX = [
    (re.compile(r'\[possible values: ([^\]]+)\]', re.I), r','),
    (
        re.compile(
            r'(?:one of|possible values|valid values(?: are)?|valid options(?: are)?|accepted values'
            r'(?: \([^)]*\))?|choices(?: are)?)\s*:?\s*([^.;\]]+)',
            re.I,
        ),
        r',\s*(?:or\s+|and\s+)?|\s+or\s+',
    ),
    (re.compile(r'\{([^{}\s]+,[^{}\s]+)\}'), r','),
    (re.compile(r'(?:^|\()([\w.+-]+(?:/[\w.+-]+)+)(?=[,;)]|$)'), r'/'),
    (
        re.compile(r"('[\w.+-]+'(?:\s*\([^)]*\))?(?:(?:\s*,\s*(?:or\s+)?|\s+or\s+)'[\w.+-]+'(?:\s*\([^)]*\))?)+)"),
        r',\s*(?:or\s+)?|\s+or\s+',
    ),
]


def _vals(items):
    items = [re.sub(r'\s*\([^)]*\)', '', i).strip().strip('\'"`') for i in items]
    ok = 2 <= len(items) <= 40 and all(len(i) <= 32 and _SAFE_VAL.fullmatch(i) for i in items)
    return list(dict.fromkeys(items)) if ok else []


def _desc_vals(d):
    for rx, sep in _VALRX:
        for m in rx.finditer(d):
            v = _vals(re.split(sep, m.group(1).strip()))
            if v:
                return v
    return []


def _finish(o):
    """Normalize desc, derive value lists and file-ness of the argument."""
    o['d'] = re.sub(r'\s+', ' ', o['d']).strip()
    o.setdefault('req', False)
    if not o['arg'] and re.match(r'\(default (?!true\)|false\))', o['d']):
        o['arg'] = (o['l'] or o['o'] or o['s'])[0]
    if o.get('vals') or o.get('file') or not o['arg']:
        o.setdefault('vals', [])
        o.setdefault('file', False)
        return o
    arg = o['arg']
    name = _unwrap(arg)
    vals = _vals(re.split(r'[|,]', name)) if '|' in arg or ('{' in arg and ',' in arg) else []
    generic = not name or name.lower() in _GENERIC
    file = not vals and not generic and bool(_FILEISH.search(name))
    if not vals and not file:
        vals = _desc_vals(o['d'])
        file = not vals and generic and bool(_FILEISH.search(' '.join(o['l'] + o['o'])))
    o.update(arg=name or arg, vals=vals, file=file)
    return o


def _names(o):
    return {f'-{s}' for s in o['s']} | {f'--{l}' for l in o['l']} | {f'-{x}' for x in o['o']}


def _dedupe(opts):
    seen, out = set(), []
    for o in opts:
        n = _names(o)
        if n and not n & seen:
            seen |= n
            out.append(o)
    return out[:400]


def _top_tokens(v):
    toks, cur, depth = [], '', 0
    for c in v:
        if c in '[({<':
            depth += 1
        elif c in '])}>':
            depth = max(depth - 1, 0)
        if c.isspace() and depth == 0:
            if cur:
                toks.append(cur)
            cur = ''
        else:
            cur += c
    return toks + [cur] if cur else toks


def _uarg(t):
    return bool(t) and (t[0] == '<' or bool(re.fullmatch(r'[A-Za-z][\w.:-]*(\.\.\.)?', t)))


def _unwrap(a):
    """'<file>...' -> 'file'; only a single enclosing group is removed ("<x>,<y>" stays)."""
    a = a.removesuffix('...').lstrip('=')
    m = re.fullmatch(r'<([^<>]*)>|\[=?([^\[\]]*)\]|\{([^{}]*)\}', a)
    return next_of(g for g in m.groups() if g is not None) if m else a


def _ph(a):
    return _unwrap(a) or 'value'


def _usage_items(v):
    """One usage variant -> required items: ('lit', word) | ('flag', flag, argname|None) | ('arg', name)."""
    toks = _top_tokens(v)[1:]
    if '|' in toks:
        return None
    items, i = [], 0
    while i < len(toks):
        t = toks[i]
        if _isflag(t) and not t.startswith(('-[', '--[')):
            f, _, a = t.partition('=')
            if not a and i + 1 < len(toks) and _uarg(toks[i + 1]) and not toks[i + 1].startswith('-'):
                a = toks[i + 1]
                i += 1
            items.append(('flag', f, _ph(a) if a else None))
        elif t[0] == '<' or re.fullmatch(r'[A-Z][A-Z0-9_-]*(\.\.\.)?', t):
            items.append(('arg', _ph(t)))
        elif _SUBWORD.fullmatch(t):
            items.append(('lit', t) if all(x[0] == 'lit' for x in items) else ('arg', t))
        i += 1
    return items


def _usage_help(lines):
    for i, l in enumerate(lines):
        m = re.match(r'\s*usage:\s*(.*)', l, re.I)
        if not m:
            continue
        out, base = [], _ind(l)
        if m.group(1).strip():
            out.append(m.group(1).strip())
            for l2 in lines[i + 1 :]:
                s = l2.strip()
                m2 = re.match(r'or:\s*(.*)', s)
                if m2:
                    out.append(m2.group(1))
                elif _ind(l2) >= 8 and re.match(r'[\[<{(|-]|[A-Z][A-Z0-9_]+\b', s):
                    out[-1] += ' ' + s
                else:
                    break
        else:
            for l2 in lines[i + 1 :]:
                s = l2.strip()
                if not s:
                    if out:
                        break
                    continue
                if _ind(l2) <= base:
                    break
                out.append(s.removeprefix('$ '))
        return out
    return []


def _usage_man(lines):
    body = [l for l in lines if l.strip()]
    if not body:
        return []
    base, out = min(_ind(l) for l in body), []
    for l in body:
        if _ind(l) == base or not out:
            out.append(l.strip())
        else:
            out[-1] += ' ' + l.strip()
    return out


_SUBHDR = re.compile(r'\s*(?:(?:available|additional)\s+)?(?:sub)?commands(?: are)?:?|\s*the commands are:', re.I)
_SUBENT = re.compile(r'\s+([a-z][\w.-]*)(?:,\s*[\w.-]+)*\s{2,}(\S.*)')


def _scan_subs(lines):
    subs, active, base, got = [], False, 0, False
    for l in lines:
        m = re.match(r'\s+\{([\w,.-]+)\}', l)
        if m and ',' in m.group(1):
            subs += [(s, '') for s in m.group(1).split(',')]
        if _SUBHDR.fullmatch(l):
            active, base, got = True, _ind(l), False
            continue
        if not active:
            continue
        if not l.strip():
            active = not got
            continue
        m = _SUBENT.fullmatch(l)
        if m and _ind(l) > base:
            subs.append((m.group(1), _clean_desc(m.group(2), 80)))
            got = True
        else:
            active = False
    out = {}
    for s, d in subs:
        out.setdefault(s, d)
    return list(out.items())


def _man_sections(lines):
    secs, name = [], ''
    for l in lines[1:]:
        if l and not l[0].isspace() and re.fullmatch(r'[A-Z][A-Z0-9 ,/&-]*', l):
            name = l
            secs.append((name, []))
        elif secs:
            secs[-1][1].append(l)
    return secs


def parse_help(text, man=False):
    """--help / -h output or a rendered man page -> {'opts', 'usage', 'subs'}."""
    lines = _lines(text)
    if man:
        secs = _man_sections(lines)
        usage = next_of(_usage_man(ls) for n, ls in secs if n == 'SYNOPSIS') or []
        body = [l for n, ls in secs if n not in ('NAME', 'SYNOPSIS', 'EXAMPLE', 'EXAMPLES', 'SEE ALSO') for l in ls + ['']]
        subs = []
    else:
        usage, body, subs = _usage_help(lines), lines, _scan_subs(lines)
    variants = [it for it in map(_usage_items, usage) if it is not None]
    opts = _dedupe(_finish(o) for o in _scan_opts(body))
    flagsets = [{x[1] for x in it if x[0] == 'flag'} for it in variants]
    req = set.intersection(*flagsets) if flagsets else set()
    for o in opts:
        o['req'] = o.get('req') or bool(req & _names(o))
    return {'opts': opts, 'usage': variants[:20], 'subs': subs}


def _useful(info):
    return bool(info and (info.get('opts') or info.get('subs') or any(info.get('usage') or [])))


def _merge(*infos):
    out = {'opts': [], 'usage': [], 'subs': []}
    for i in infos:
        if not i:
            continue
        out['opts'] += i.get('opts', [])
        out['usage'] = out['usage'] or i.get('usage', [])
        out['subs'] += [s for s in i.get('subs', []) if s[0] not in {x[0] for x in out['subs']}]
    out['opts'] = _dedupe(_finish(o) for o in out['opts'])
    return out


def _words(s):
    import shlex

    try:
        return shlex.split(s, comments=True)
    except ValueError:
        return s.split()


def _case_flag(w, takes=False):
    w = w.strip('\'"')
    if w.endswith('=*'):
        w, takes = w[:-2], True
    m = re.fullmatch(r'--?[\w?][\w.-]*', w) and _OPTW.fullmatch(w)
    if not m:
        return None
    o = _new_opt()
    _add_name(o, m)
    o['arg'] = (o['l'] or o['o'] or ['value'])[0] if takes else None
    return o


def _join(opts):
    """Aliases from one `case` pattern list -> one option."""
    opts = [o for o in opts if o]
    if not opts:
        return None
    o = opts[0]
    for x in opts[1:]:
        o['s'], o['l'], o['o'] = o['s'] + x['s'], o['l'] + x['l'], o['o'] + x['o']
        o['arg'] = o['arg'] or x['arg']
    if o['arg']:
        o['arg'] = (o['l'] or o['o'] or [o['arg']])[0]
    return o


def _argparse_spec(spec):
    m = re.fullmatch(r'([\w/-]*)(?:#([\w-]*))?(=[?+*]?)?&?(?:!.*)?', spec)
    if not m:
        return None
    head, num, eq = m.groups()
    o = _new_opt()
    if num is not None:
        o['s'], o['l'], o['arg'] = [head[:1]] if head else [], [num] if num else [], 'number'
    elif len(head) == 1:
        o['s'] = [head]
    elif len(head) > 2 and head[1] == '/':
        o['s'], o['l'] = [head[0]], [head[2:]]
    elif len(head) > 2 and head[1] == '-':
        o['l'] = [head[2:]]
    elif len(head) > 1:
        o['l'] = [head]
    else:
        return None
    if eq:
        o['arg'], o['opt'] = (o['l'] or ['value'])[0], eq in ('=?', '=*')
    return o


def parse_fish_source(text, name=None):
    if name:
        m = re.search(rf'^[ \t]*function[ \t]+{re.escape(name)}(?=[\s;])', text, re.M)
        if m:
            rest = text[m.end() :]
            n = re.search(r'^function[ \t]', rest, re.M)
            text = rest[: n.start()] if n else rest
    opts, subs, on_arg1 = [], [], False
    for line in re.sub(r'\\\n', ' ', text).splitlines():
        s = line.strip()
        if re.match(r'(?:(?:and|or|not|if|else if|set\s+-\w+\s+\w+)\s+)*argparse\s', s):
            words = _words(s)
            words, skip = words[words.index('argparse') + 1 :], False
            for w in words:
                if skip:
                    skip = False
                elif w == '--':
                    break
                elif w.startswith('-'):
                    skip = w in ('-n', '--name', '-x', '--exclusive', '-N', '--min-args', '-X', '--max-args')
                else:
                    opts.append(_argparse_spec(w))
        elif s.startswith('switch '):
            on_arg1 = bool(re.fullmatch(r'switch\s+"?\$argv\[1\]"?(\s*#.*)?', s))
        elif s.startswith('case '):
            ws = _words(s[5:])
            opts.append(_join(_case_flag(w) for w in ws if w.startswith('-')))
            subs += [(w, '') for w in ws if on_arg1 and _SUBWORD.fullmatch(w)]
    return _merge({'opts': [o for o in opts if o], 'usage': [], 'subs': subs})


def parse_sh_source(text):
    opts, subs, target = [], [], None
    for m in re.finditer(r'\bgetopts\s+["\']?:?([A-Za-z0-9:]+)', text):
        spec = m.group(1)
        for i, c in enumerate(spec):
            if c != ':':
                o = _new_opt()
                o['s'], o['arg'] = [c], 'value' if spec[i + 1 : i + 2] == ':' else None
                opts.append(o)
    lines = text.splitlines()
    for i, l in enumerate(lines):
        mc = re.match(r'\s*case\s+"?\$\{?(\w+)', l)
        if mc:
            target = mc.group(1)
            continue
        if re.match(r'\s*esac\b', l):
            target = None
            continue
        mp = re.match(r'\s*\(?\s*([^\s()|;]+(?:\s*\|\s*[^\s()|;]+)*)\s*\)(.*)', l)
        if not mp or target is None:
            continue
        body = '\n'.join([mp.group(2)] + lines[i + 1 : i + 15]).split(';;', 1)[0]
        takes = bool(re.search(r'\$\{?2\b|shift\s+2|OPTARG', body))
        ws = [w.strip().strip('"\'') for w in mp.group(1).split('|')]
        opts.append(_join(_case_flag(w, takes) for w in ws if w.startswith('-')))
        subs += [(w, '') for w in ws if target in SUB_VARS and _SUBWORD.fullmatch(w)]
    docs = [parse_help(m.group(3)) for m in re.finditer(r'<<-?\s*([\'"]?)(\w+)\1[^\n]*\n(.*?)^\s*\2\s*$', text, re.S | re.M)]
    return _merge(*docs, {'opts': [o for o in opts if o], 'usage': [], 'subs': subs})


def _lit(node):
    import ast

    try:
        return ast.literal_eval(node) if node is not None else None
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def parse_py_source(text):
    import ast

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    opts, subs, docs = [], [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and 'usage' in node.value.lower() and '\n' in node.value:
            docs.append(parse_help(node.value))
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        args = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if node.func.attr == 'add_parser' and args:
            subs.append((args[0], _clean_desc(str(_lit(kw.get('help')) or ''), 80)))
        elif node.func.attr in ('add_argument', 'add_option') and args and all(_isflag(a) for a in args):
            o = _new_opt()
            for a in args:
                if m := _OPTW.fullmatch(a):
                    _add_name(o, m)
            act = kw.get('action')
            boolean = isinstance(act, ast.Attribute) and act.attr == 'BooleanOptionalAction'
            if boolean:
                o['l'] += [f'no-{l}' for l in o['l']]
            elif (
                _lit(act) not in ('store_true', 'store_false', 'store_const', 'count', 'help', 'version', 'append_const')
                and _lit(kw.get('nargs')) != 0
            ):
                o['arg'] = str(_lit(kw.get('metavar')) or (o['l'] or o['o'] or ['value'])[0])
                choices = _lit(kw.get('choices'))
                if isinstance(choices, (list, tuple, set, range)):
                    o['vals'] = _vals([str(c) for c in choices]) or []
                t = getattr(kw.get('type'), 'func', kw.get('type'))
                o['file'] = getattr(t, 'attr', getattr(t, 'id', None)) in ('Path', 'FileType') and not o.get('vals')
            o['req'] = _lit(kw.get('required')) is True
            o['d'] = str(_lit(kw.get('help')) or '').replace('%(default)s', '…')
            opts.append(o)
    return _merge({'opts': opts, 'usage': [], 'subs': subs}, *docs)


def _man(name):
    import subprocess

    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'MANPATH')}
    env.update(LANG='C', LC_ALL='C', MANPAGER='cat', PAGER='cat', MANWIDTH='200')
    try:
        r = subprocess.run(['man', '-S', '1:8:6', name], stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ''
    return r.stdout.decode('utf-8', 'replace')[: 1 << 20] if r.returncode == 0 else ''


def _mentions_help(path):
    """`help` or `usage` as a word somewhere in the binary. A program without either can't be answering
    --help: it ignores its arguments and does its job (open a serial port, start a stream)."""
    import mmap

    with open(path, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        for word in (b'help', b'Help', b'HELP', b'usage', b'Usage', b'USAGE'):
            i = m.find(word)
            for _ in range(10000):
                if i < 0:
                    break
                before, after = m[i - 1 : i] if i else b'', m[i + len(word) : i + len(word) + 1]
                if not before.isalpha() and not (after.isalpha() and after.islower()):
                    return True
                i = m.find(word, i + 1)
    return False


def exec_ok(path, name):
    """Spec safety rules for running `PATH --help`: ELF, on $PATH, not setuid/setcap, not deny-listed, and
    the binary says `help` or `usage` somewhere."""
    try:
        real = os.path.realpath(path)
        names = {name, os.path.basename(path), os.path.basename(real)}
        if any(n in DENY or n.startswith(('mkfs', 'kill')) for n in names):
            return False
        dirs = {os.path.realpath(d) for d in os.environ.get('PATH', '').split(':') if d}
        if os.path.realpath(os.path.dirname(os.path.abspath(path))) not in dirs:
            return False
        import stat

        st = os.stat(real)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & (stat.S_ISUID | stat.S_ISGID):
            return False
        try:
            if os.getxattr(real, 'security.capability'):
                return False
        except OSError:
            pass
        with open(real, 'rb') as f:
            return f.read(4) == b'\x7fELF' and _mentions_help(real)
    except (OSError, ValueError):
        return False


def _limits():
    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _run_help(path):
    import signal
    import subprocess
    import tempfile

    info = {}
    with tempfile.TemporaryDirectory(prefix='fishsense-help-') as tmp:
        env = {
            'PATH': os.environ.get('PATH', '/usr/bin'),
            'LANG': 'C',
            'LC_ALL': 'C',
            'HOME': tmp,
            'TERM': 'dumb',
            'NO_COLOR': '1',
            'COLUMNS': '200',
            'XDG_CONFIG_HOME': tmp,
            'XDG_CACHE_HOME': tmp,
            'XDG_DATA_HOME': tmp,
            'XDG_STATE_HOME': tmp,
            'XDG_RUNTIME_DIR': tmp,
        }
        for flag in ('--help', '-h'):
            with tempfile.TemporaryFile() as out:
                p = subprocess.Popen(
                    [path, flag],
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=out,
                    cwd=tmp,
                    env=env,
                    start_new_session=True,
                    preexec_fn=_limits,
                )
                try:
                    p.wait(timeout=HELP_TIMEOUT)
                    hung = False
                except subprocess.TimeoutExpired:
                    hung = True
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
                out.seek(0)
                info = parse_help(out.read(1 << 19).decode('utf-8', 'replace'))
            if hung or _useful(info):
                break
    return info


def _parse_target(name, path, kind, allow_exec):
    """-> (info, complete); complete=False when an ELF's --help was not tried yet."""

    def read():
        return Path(path).read_bytes()[: 2 << 20].decode('utf-8', 'replace')

    if kind == 'function':
        return {**parse_fish_source(read(), name), 'src': 'source'}, True
    man = _man(name)
    if man:
        info = parse_help(man, man=True)
        if _useful(info):
            return {**info, 'src': 'man'}, True
    with open(path, 'rb') as f:
        head = f.read(4096)
    if head[:4] == b'\x7fELF':
        if allow_exec and exec_ok(path, name):
            return {**_run_help(path), 'src': '--help'}, True
        return {}, allow_exec
    if b'\0' in head:
        return {}, True
    first = head.split(b'\n', 1)[0] if head.startswith(b'#!') else b''
    if b'python' in first or (not first and path.endswith('.py')):
        return {**parse_py_source(read()), 'src': 'source'}, True
    if b'fish' in first or (not first and path.endswith('.fish')):
        return {**parse_fish_source(read()), 'src': 'source'}, True
    if not first or re.search(rb'\b(ba|z|da|k|mk)?sh\b', first):
        return {**parse_sh_source(read()), 'src': 'source'}, True
    return {}, True


def _key(path):
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    return f'{os.path.realpath(path)}:{st.st_mtime_ns}:{st.st_size}' if os.path.isfile(path) else None


def _load(p):
    try:
        return json.loads(Path(p).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def help_info(name, path=None, kind='file', allow_exec=False, force=False):
    """Parsed options/usage/subcommands for a command, cached by path+mtime+size. Executes only when allowed."""
    if not _NAME.fullmatch(name or ''):
        return {}
    if kind == 'wraps':
        kind = 'function' if path and path.endswith('.fish') else 'file'
    if kind != 'function' and not path:
        import shutil

        path = shutil.which(name)
    cpath = common.cache_dir() / 'help' / f'{name}.json'
    key, cached = _key(path), _load(cpath)
    if cached and not force and (key is None or cached.get('key') == key) and (cached.get('complete') or not allow_exec):
        return cached
    if key is None:
        return cached or {}
    try:
        info, complete = _parse_target(name, path, kind, allow_exec)
    except OSError:
        return cached or {}
    info = {'opts': [], 'usage': [], 'subs': [], 'src': '', **info, 'key': key, 'complete': complete}
    common.write_atomic(cpath, json.dumps(info, ensure_ascii=False))
    return info


def _fish_ships(name):
    """A completion fish (embedded or vendor) or the user already has; ours would only add noise."""
    import subprocess

    dirs = [
        Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'fish/completions',
        Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'fish/vendor_completions.d',
    ]
    for d in (os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share').split(':'):
        dirs += [Path(d) / 'fish/vendor_completions.d', Path(d) / 'fish/completions']
    if any((d / f'{name}.fish').is_file() for d in dirs):
        return True
    try:
        return (
            subprocess.run(
                ['fish', '--no-config', '-c', 'status list-files completions/$argv[1].fish', name],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=3,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def render_completions(name, info):
    """`complete -c NAME …` lines, every string single-quoted; '' when there is nothing to complete."""
    head = f'# fishsense: generated from {info.get("src") or "help"}; regenerated automatically\n'
    lines, c = [], _q(name)
    for o in info.get('opts', []):
        parts = ['complete', '-c', c]
        parts += [x for s in o['s'] if len(s) == 1 and s != '-' for x in ('-s', _q(s))]
        parts += [x for l in o['l'] if _LONG.fullmatch(l) for x in ('-l', _q(l))]
        parts += [x for l in o['o'] if _LONG.fullmatch(l) for x in ('-o', _q(l))]
        if len(parts) == 3:
            continue
        vals = [v for v in o.get('vals', []) if _SAFE_VAL.fullmatch(v)]
        if o.get('arg'):
            if o.get('file'):
                parts += ['-F'] if o.get('opt') else ['-r', '-F']
            elif vals:
                parts += ([] if o.get('opt') else ['-x']) + ['-a', _q(' '.join(vals))]
            elif not o.get('opt'):
                parts.append('-r')
        d = _clean_desc(o.get('d', ''))
        d = f'(required) {d}'.strip() if o.get('req') else d
        if d:
            parts += ['-d', _q(d)]
        lines.append(' '.join(parts))
    for sub, d in info.get('subs', []):
        if _SAFE_VAL.fullmatch(sub):
            d = _clean_desc(d)
            lines.append(f"complete -c {c} -n '__fish_use_subcommand' -a {_q(sub)}" + (f' -d {_q(d)}' if d else ''))
    return head + '\n'.join(lines) + '\n' if lines else ''


def compgen(name, kind='file', path=None, force=False):
    """Write completions for a command fish has none for. -> path of the .fish file, or None."""
    if not _NAME.fullmatch(name or '') or _fish_ships(name):
        return None
    text = render_completions(name, help_info(name, path, kind, allow_exec=True, force=force))
    if not text:
        return None
    out = common.cache_dir() / 'completions' / f'{name}.fish'
    try:
        same = out.read_text(encoding='utf-8') == text
    except OSError:
        same = False
    if not same:
        common.write_atomic(out, text)
    return out


def tldr_dir():
    return common.cache_dir() / 'tldr'


def parse_tldr(text):
    """[(description, command)] in page order."""
    out, desc = [], None
    for l in text.splitlines():
        s = l.strip()
        if s.startswith('- '):
            desc = s[2:].rstrip(':')
        elif desc is not None and len(s) > 2 and s[0] == s[-1] == '`':
            out.append((common.sanitize(desc), common.sanitize(s[1:-1])))
            desc = None
    return out


def _alts(tok):
    m = re.fullmatch(r'\{\{\[([^\]]*\|[^\]]*)\]\}\}', tok)
    return m.group(1).split('|') if m else None


def _render(tok):
    def sub(m):
        a = _alts(m.group(0))
        return a[0] if a else f'‹{m.group(1)}›'

    return re.sub(r'\{\{(.*?)\}\}', sub, tok)


def _ex_words(cmd):
    w = _split(cmd) or []
    while w and w[0] in ('sudo', 'doas', 'run0', 'pkexec'):
        w = w[1:]
    return w


def _tldr_match(words, cmd):
    """Remainder of a tldr example after the buffer's words, or None if they disagree."""
    et = _ex_words(cmd)
    if not et or len(et) <= len(words):
        return None
    for b, e in zip(words, et):
        a = _alts(e)
        if a is not None:
            if b not in a:
                return None
        elif '{{' not in e and b != e and b.strip('\'"') != e.strip('\'"'):
            return None
    return ' '.join(_render(t) for t in et[len(words) :])


def _page(name, lang):
    if not _NAME.fullmatch(name):
        return None
    for plat in PLATFORMS:
        try:
            return (tldr_dir() / lang / plat / f'{name}.md').read_text(encoding='utf-8', errors='replace')
        except OSError:
            pass
    return None


def _structure(name, examples):
    """Best whole-command template of a page: first example with a placeholder that starts with the name."""
    for _, cmd in examples:
        w = _ex_words(cmd)
        if w and w[0] == name and any('{{' in t and _alts(t) is None for t in w[1:]):
            return ' '.join(_render(t) for t in w)
    return None


def build_index(root):
    static, pages = {}, {}
    for lang in reversed(LANGS):
        pages[lang] = 0
        for plat in reversed(PLATFORMS):
            for p in sorted((Path(root) / lang / plat).glob('*.md')):
                pages[lang] += 1
                line = _structure(p.stem, parse_tldr(p.read_text(encoding='utf-8', errors='replace')))
                if line:
                    static[p.stem] = line
    common.write_atomic(
        Path(root) / 'index.json',
        json.dumps({'v': 2, 'updated': int(time.time()), 'pages': pages, 'static': static}, ensure_ascii=False),
    )
    return sum(pages.values())


def tldr_update():
    """Download the English pages (common/ and linux/ only), rebuild the index, swap in atomically. -> page count."""
    import io
    import shutil
    import urllib.request
    import zipfile

    root = tldr_dir()
    tmp, old = root.with_name(f'.tldr.{os.getpid()}'), root.with_name(f'.tldr-old.{os.getpid()}')
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        for lang in LANGS:
            with urllib.request.urlopen(TLDR_URL.format(lang), timeout=30) as r:
                data = r.read(64 << 20)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist()[:50000]:
                    parts = info.filename.split('/')
                    if (
                        len(parts) == 2
                        and parts[0] in PLATFORMS
                        and parts[1].endswith('.md')
                        and _NAME.fullmatch(parts[1][:-3])
                        and info.file_size < 1 << 20
                    ):
                        dest = tmp / lang / parts[0] / parts[1]
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(z.read(info))
        n = build_index(tmp)
        if root.exists():
            os.rename(root, old)
        os.rename(tmp, root)
        shutil.rmtree(old, ignore_errors=True)
        return n
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _tldr_next(words):
    lead = [words[0]]
    for w in words[1:]:
        if _isflag(w):
            break
        lead.append(w)
    for k in range(len(lead), 0, -1):
        for lang in LANGS:
            page = _page('-'.join(lead[:k]), lang)
            for _, cmd in parse_tldr(page or ''):
                r = _tldr_match(words, cmd)
                if r:
                    return r
    return None


def static_items(exec_names):
    """One structure line per installed command that has a tldr page or cached help."""
    exec_names = set(exec_names)
    idx = _load(tldr_dir() / 'index.json') or {}
    st = idx.get('static', {}) if idx.get('v') == 2 else {}
    out = {n: st[n] for n in exec_names & st.keys()}
    rest = exec_names.difference(out)
    try:
        cached = [p for p in (common.cache_dir() / 'help').glob('*.json') if p.stem in rest]
    except OSError:
        cached = []
    for p in cached:
        items = next_of((_load(p) or {}).get('usage') or [])
        if items:
            out[p.stem] = ' '.join([p.stem] + [_item(x) for x in items])
    return [common.sanitize(out[n]) for n in sorted(out)]


def _item(x):
    if x[0] == 'flag':
        return x[1] + (f' ‹{x[2]}›' if x[2] else '')
    return x[1] if x[0] == 'lit' else f'‹{x[1]}›'


def _arg_map(info):
    """flag spelling -> (argname or None, every spelling of that option)."""
    m = {}
    for o in info.get('opts', []):
        names = _names(o)
        for n in names:
            m[n] = (None if o.get('opt') else o.get('arg'), names)
    for it in info.get('usage', []):
        for x in it:
            if x[0] == 'flag' and x[1] not in m:
                m[x[1]] = (x[2], {x[1]})
    return m


def _habit_flag(words, history, amap):
    """Next flag the user usually adds after these (seen in ≥2 history lines), with its value placeholder."""
    c, bflags = words[0], {_flag(w) for w in words[1:] if _isflag(w)}
    lead = []
    for w in words[1:]:
        if _isflag(w):
            break
        lead.append(w)
    counts, newest, valued, total = {}, {}, 0, 0
    last = _flag(words[-1]) if _isflag(words[-1]) and '=' not in words[-1] else None
    word = re.compile(rf'(?:^|[\s;|&(/]){re.escape(c)}(?:[\s;|&)]|$)')
    for age, h in enumerate(reversed(history)):
        if not word.search(h):
            continue
        for seg in _segments(h):
            if not seg or seg[0] != c or seg[1 : 1 + len(lead)] != lead:
                continue
            hflags = [_flag(w) for w in seg[1:] if _isflag(w)]
            if not bflags <= set(hflags):
                continue
            if last:
                j = next_of(i for i, w in enumerate(seg) if _flag(w) == last)
                total += 1
                valued += j + 1 < len(seg) and not _isflag(seg[j + 1]) and '=' not in seg[j]
            f = next_of(f for f in hflags if f not in bflags)
            if f:
                counts[f] = counts.get(f, 0) + 1
                newest.setdefault(f, age)
    if not counts or (last and valued * 2 >= total):
        return None
    best = max(counts, key=lambda f: (counts[f], -newest[f]))
    if counts[best] < 2:
        return None
    arg = amap.get(best, (None,))[0]
    return best + (f' ‹{arg}›' if arg else '')


def _usage_next(words, info, amap):
    present, pos, i = set(), [], 1
    while i < len(words):
        w = words[i]
        if _isflag(w):
            present |= amap.get(_flag(w), (None, {_flag(w)}))[1]
            if '=' not in w and amap.get(_flag(w), (None,))[0]:
                i += 1
        else:
            pos.append(w)
        i += 1
    for items in info.get('usage', []):
        lits = [x[1] for x in items if x[0] == 'lit']
        if pos[: len(lits)] != lits:
            continue
        nargs, k, miss = len(pos) - len(lits), 0, []
        for x in items:
            if x[0] == 'flag' and x[1] not in present:
                miss.append(_item(x))
            elif x[0] == 'arg':
                if k >= nargs:
                    miss.append(_item(x))
                k += 1
        return ' '.join(miss) or None
    return None


def next(buffer, cwd=None, history=(), target=None):
    """Continuation for a buffer ending in a space: the user's usage -> tldr -> Usage/SYNOPSIS.

    Returns the full line (buffer + continuation, may hold ‹…› placeholders) or None. Nothing is offered
    when the history already extends the buffer: the ranked native suggestion shows that one.
    target = (name, kind, path) fish passed for the command, used to find its parsed help.
    cwd is part of the CLI contract but nothing here depends on it yet.
    """
    if not buffer.endswith(' ') or not buffer.strip():
        return None
    segs = _segments(buffer)
    words = segs[-1] if segs else None
    if not words or any(h.startswith(buffer) and len(h) > len(buffer) for h in history):
        return None
    c = words[0]
    name, kind, path = target if target and target[0] == c else (c, 'file', None)
    info = help_info(c, path, kind)
    amap = _arg_map(info)
    last = words[-1]
    pend = amap.get(_flag(last), (None,))[0] if _isflag(last) and '=' not in last else None
    import shutil

    tldr = _tldr_next(words) if shutil.which(c) else None
    cont = (
        (None if pend else _habit_flag(words, history, amap)) or tldr or (f'‹{pend}›' if pend else _usage_next(words, info, amap))
    )
    if not cont:
        return None
    line = buffer + common.sanitize(cont)
    return line if line.startswith(buffer) else None


_BOOL = {'--notify', '--no-completions', '--force'}


def _opts(argv):
    o, i = {}, 0
    while i < len(argv):
        k = argv[i]
        if k in _BOOL:
            o[k[2:]] = True
        elif k.startswith('--') and i + 1 < len(argv):
            o[k[2:]] = argv[i + 1]
            i += 1
        i += 1
    return o


def _notify(pid, kind, req, payload):
    f = common.run_dir() / f'{pid}.result'
    for _ in range(50):
        try:
            if not f.stat().st_size:
                break
        except OSError:
            break
        time.sleep(0.02)
    common.notify(pid, kind, req, payload)


def main(sub, argv):
    o = _opts(argv)

    def emit(kind, payload):
        if o.get('notify') and o.get('pid'):
            _notify(o['pid'], kind, o.get('id', '0'), payload)
        else:
            print(f'{kind}\t{common.enc(payload)}', flush=True)

    if sub == 'next':
        hist = []
        if o.get('fish-data'):
            hist = [i.cmd for i in common.read_fish_history(common.real_history_path(o['fish-data'], o.get('real-name', 'fish')))]
        buf, cwd = common.dec(o.get('buffer', '')), common.dec(o.get('cwd', ''))
        name = o.get('cmd-name')
        target = (name, o.get('cmd-kind', 'file'), o.get('cmd-path')) if name else None
        line = next(buf, cwd, hist, target)
        if line:
            emit('ghost', line)
        if o.get('no-completions') and name:
            p = compgen(name, o.get('cmd-kind', 'file'), o.get('cmd-path'))
            if p:
                emit('compgen', str(p))
                if not line and (line := next(buf, cwd, hist, target)):
                    emit('ghost', line)
    elif sub == 'compgen':
        p = compgen(o.get('cmd-name', ''), o.get('cmd-kind', 'file'), o.get('cmd-path'), o.get('force', False))
        if p:
            emit('compgen', str(p))
    elif sub == 'tldr-update':
        emit('ok', str(tldr_update()))
    return 0


def doctor_lines():
    idx = _load(tldr_dir() / 'index.json')
    if idx and idx.get('v') == 2:
        p = idx.get('pages', {})
        when = time.strftime('%Y-%m-%d', time.localtime(idx.get('updated', 0)))
        out = [f'tldr: {p.get("en", 0)} English pages (updated {when})']
    else:
        out = ['tldr: not downloaded; run `fishsense update`']
    try:
        n = len(list((common.cache_dir() / 'completions').glob('*.fish')))
    except OSError:
        n = 0
    return out + [f'generated completions: {n}']
