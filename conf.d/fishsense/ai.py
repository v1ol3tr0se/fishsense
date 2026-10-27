"""`ai` subcommand: one fish command line from a local ollama model, plus its doctor checks."""
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import common

DEFAULT_MODEL = 'qwen2.5-coder:14b'
BATTERY_MODEL = 'qwen2.5-coder:3b'
TIMEOUT = 20
KEEP_ALIVE = '1h'
POWER_DIR = Path('/sys/class/power_supply')
SETUP = 'run: fishsense setup-ai'

SYSTEM = (
    'You complete command lines for the fish shell on Linux. Reply with exactly one fish command line '
    'and nothing else: no explanation, no markdown, no quotes around it, no prompt sign.\n'
    '- `line` is the start of a command: reply with the whole line, keeping what was typed. Finish the word '
    'being typed, then add the arguments and flags that most likely come next. Never reply with the line '
    'unchanged: a line that already runs gets the arguments or flags the user most likely wants.\n'
    '- `line` is a request in plain words (English or Spanish): reply with the command that does it.\n'
    '- `line` is (empty): reply with the command most likely run next.\n'
    'Ground the answer in the context: `history` is what the user usually runs in this situation, `recent` is what '
    'the user just ran, `files` and `git` are the current directory, and `target` is the host being tested (use it '
    'as the IP/host for scans like nmap, gobuster or nxc). Use only commands that exist and files that are '
    'listed; never invent paths. Never invent an IP, host or domain: when a command needs a host and no '
    '`target` is given, write ‹target› there for the user to fill in. A command shown under `command:` is one '
    'the user wrote (a function or script): use only its description and how the user ran it before, never invent '
    'flags for it or split its name; if you do not know what to add, reply with just the command.'
)

EXAMPLES = '''\
cwd: ~/notes
files: draft.md images/ talk.md talk.pdf
recent:
  nvim draft.md
history:
  pandoc talk.md -o talk.pdf
line: pandoc 
answer: pandoc draft.md -o draft.pdf

cwd: ~/Projects/web
files: README.md node_modules/ package.json src/
git:
  ## main...origin/main [ahead 1]
  M  src/login.js
recent:
  npm run dev
  git add src/login.js
line: git com
answer: git commit -m "update login form"

cwd: ~
files: Documents/ Downloads/ Music/
recent:
  cd ~
line: df 
answer: df -h

cwd: ~/Projects/site
files: build/ deploy.sh index.html
line: bash de
answer: bash deploy.sh

cwd: ~/Projects/shop
files: Dockerfile compose.yaml src/
recent:
  nvim compose.yaml
  docker compose build
line: (empty)
answer: docker compose up -d

cwd: ~/Pictures
files: photos/ wallpapers/ avatar.png
line: comprime la carpeta photos
answer: tar -czf photos.tar.gz photos/

cwd: ~/lab
target: 203.0.113.10  vpn: 198.51.100.7
history:
  sudo nmap -sV -O -T4 203.0.113.9
line: sudo nmap
answer: sudo nmap -sV -O -T4 203.0.113.10

cwd: ~/work
files: notes.md
line: nmap
answer: nmap -sV ‹target›

cwd: ~/Pictures/art
files: logo.png sketch.png
history:
  waifu2x -i sketch.png -o sketch_2x.png -s 2
line: waifu2x -i logo.png
answer: waifu2x -i logo.png -o logo_2x.png -s 2'''
SHOTS = [ex.rsplit('\nanswer: ', 1) for ex in EXAMPLES.split('\n\n')]

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def model():
    m = os.environ.get('FISHSENSE_AI_MODEL', '').strip()
    return m or (BATTERY_MODEL if on_battery() else DEFAULT_MODEL)


def base_url():
    """OLLAMA_HOST read like ollama's own client does: [scheme://]host[:port][/prefix]."""
    raw = os.environ.get('OLLAMA_HOST', '').strip()
    scheme, sep, rest = raw.partition('://')
    if not sep:
        scheme, rest = 'http', raw
    port = (443 if scheme == 'https' else 80) if sep else 11434
    u = urllib.parse.urlsplit('//' + rest)
    host = u.hostname or '127.0.0.1'
    if ':' in host:
        host = f'[{host}]'
    return f'{scheme}://{host}:{u.port or port}{u.path.rstrip("/")}'


def _read(p):
    try:
        return p.read_text().strip()
    except OSError:
        return ''


def on_battery():
    """A Mains supply exists and none is online. Then the model stays on the CPU so the dGPU can sleep."""
    mains = [d for d in POWER_DIR.glob('*') if _read(d / 'type') == 'Mains']
    return bool(mains) and all(_read(d / 'online') != '1' for d in mains)


def history(fish_data, real_name):
    return [i.cmd for i in common.read_fish_history(common.real_history_path(fish_data, real_name))] \
        if fish_data else []


def recent(cwd, hist, n=8):
    """Last n commands, this cwd's first (they end up nearest the question).
    Only commands still in the real history: what the user deleted there stays gone."""
    known, picked = set(hist), []
    for c in [r.cmd for r in reversed(common.read_log()) if r.cwd == cwd] + hist[::-1]:
        if c in known and c not in picked:
            picked.append(c)
            if len(picked) == n:
                break
    return picked[::-1]


def _git(cwd):
    try:
        r = subprocess.run(['git', '--no-optional-locks', '-C', cwd, 'status', '--short', '--branch'],
                           capture_output=True, encoding='utf-8', errors='replace',
                           stdin=subprocess.DEVNULL, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return []
    return r.stdout.splitlines()[:13] if r.returncode == 0 else []


def _short(path):
    home = str(Path.home())
    return '~' + path[len(home):] if path == home or path.startswith(home + '/') else path


def target():
    """The pentest target in ~/.config/target and the active VPN IP, when either exists.
    Lets the model fill in the host being scanned (nmap, gobuster, nxc...) instead of a placeholder."""
    conf = _xdg_config() / 'target'
    ip = _read(conf)
    vpn = ''
    if ip:
        try:
            r = subprocess.run(['ip', '-4', '-br', 'addr'], capture_output=True, encoding='utf-8',
                               errors='replace', stdin=subprocess.DEVNULL, timeout=1)
            for name, _, addr, *_ in (ln.split() for ln in r.stdout.splitlines() if ln.split()):
                if name.startswith(('tun', 'proton')) or 'tun' in name:
                    vpn = addr.split('/')[0]
                    break
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return ip, vpn


def _xdg_config():
    v = os.environ.get('XDG_CONFIG_HOME')
    return Path(v) if v else Path.home() / '.config'


def _one(s):
    return s.replace('\n', '; ')[:200]


def habits(typed, cmds, n=6):
    """How the user ran the command being typed before: the lines that start with the whole typed text
    first, then the most used. While the command word itself is still being typed, every command
    it can become counts."""
    words = typed.split()
    if not words:
        return []
    word, partial = words[0], len(words) == 1 and not typed[-1:].isspace()
    seen = {}
    for i, c in enumerate(cmds):
        w = c.split(None, 1)[0] if c.strip() else ''
        if w.startswith(word) if partial else w == word:
            n_, _ = seen.get(c, (0, 0))
            seen[c] = (n_ + 1, i)
    order = sorted(seen, key=lambda c: (not c.startswith(typed), -seen[c][0], -seen[c][1]))
    return [c for c in order if not typed.startswith(c)][:n]


def usual(fish_data, pid, hist, n=6):
    """With nothing typed: what the ranking puts first right now, for this directory and what the user just
    ran (the newest items of this shell's shadow history). Only lines from the real history: the shadow
    also holds templates and the ghosts injected for the line being edited, the AI's own among them."""
    if not (fish_data and pid):
        return []
    known, out = set(hist), []
    for i in reversed(common.read_fish_history(common.shadow_history_path(fish_data, pid))):
        if i.cmd in known and i.cmd not in out:
            out.append(i.cmd)
            if len(out) == n:
                break
    return out


def names(text, typed):
    """What the command word being typed can become: the user's functions, builtins and abbreviations (fish sends
    them, one per line) and $PATH."""
    out = [common.sanitize(n).strip() for n in text.splitlines()]
    out += sorted(n for n in common.path_executables() if n.startswith(typed.strip()))
    return [n for n in dict.fromkeys(out) if n]


def command_typed(typed):
    """Only the command word so far, nothing after it."""
    return bool(typed.strip()) and not any(c.isspace() for c in typed.lstrip())


def cmd_hint(typed, desc, custom):
    """A `command:` line for one of the user's own commands, so the model grounds itself in its description
    (functions) instead of inventing flags for a script the model has never seen."""
    word = typed.split()[0] if typed.split() else ''
    if not (word and custom):
        return ''
    return f'command: {word} — {desc}' if desc else f'command: {word} (a custom command; use it as-is unless you know its flags)'


def build_prompt(cwd, typed, cmds, history=(), files=None, git=None, warm=False, tgt=('', ''), cmd_hint=''):
    """The question about the line. What only depends on the directory comes first: ollama reuses the
    part it already read, and warm=True stops right there (see warm())."""
    out = [f'cwd: {_short(cwd)}']
    if tgt[0]:
        out.append(f'target: {tgt[0]}' + (f'  vpn: {tgt[1]}' if tgt[1] else ''))
    if cmd_hint:
        out.append(cmd_hint)
    if files is None:
        try:
            names = sorted(os.listdir(cwd), key=lambda n: (n.startswith('.'), n.lower()))[:40]
        except OSError:
            names = []
        files = [n + '/' * os.path.isdir(os.path.join(cwd, n)) for n in names]
    if files:
        out.append('files: ' + ' '.join(f"'{n}'" if ' ' in n else n for n in files))
    if git := _git(cwd) if git is None else git:
        out += ['git:'] + [f'  {line}' for line in git]
    if cmds:
        out += ['recent:'] + [f'  {_one(c)}' for c in cmds]
    if warm:
        return '\n'.join(out)
    if history := [c for c in history if c not in cmds]:
        out += ['history:'] + [f'  {_one(c)}' for c in history]
    out.append(f'line: {_one(typed) or "(empty)"}')
    return '\n'.join(out)


def generate(prompt, prefill='', predict=80, timeout=TIMEOUT):
    """The model's reply; with a prefill it can only continue that text (the reply leaves it out)."""
    opts = {'num_predict': predict, 'temperature': 0.2, 'stop': ['\n\n', '\n```']}
    if on_battery():
        opts['num_gpu'] = 0
    msgs = [{'role': 'system', 'content': SYSTEM}]
    for q, a in SHOTS:
        msgs += [{'role': 'user', 'content': q}, {'role': 'assistant', 'content': a}]
    msgs.append({'role': 'user', 'content': prompt})
    if prefill:
        msgs.append({'role': 'assistant', 'content': prefill})
    body = json.dumps({'model': model(), 'messages': msgs, 'stream': False, 'think': False,
                       'keep_alive': KEEP_ALIVE, 'options': opts}).encode()
    req = urllib.request.Request(base_url() + '/api/chat', body, {'Content-Type': 'application/json'})
    with _open(req, timeout=timeout) as r:
        r = json.load(r)
    return '' if r.get('done_reason') == 'length' and predict > 1 else (r.get('message') or {}).get('content') or ''


def clean(reply):
    """First real line of the reply, without fences, labels, prompt signs or wrapping quotes."""
    if '```' in reply:
        inner = reply.split('```', 2)[1]
        reply = inner.split('\n', 1)[1] if '\n' in inner else inner
    for line in reply.splitlines():
        line = common.sanitize(line.replace('\t', ' ')).strip()
        if line.lower().startswith('answer:'):
            line = line[7:].strip()
        if len(line) > 1 and line[0] == line[-1] and line[0] in '"\'`' and line[0] not in line[1:-1]:
            line = line[1:-1].strip()
        for sign in ('$ ', '❯ '):
            line = line.removeprefix(sign).lstrip()
        if line:
            return line
    return ''


def decide(buffer, reply):
    """(kind, line), or None when the reply adds nothing to what is typed."""
    typed = buffer.lstrip()
    lead = buffer[:len(buffer) - len(typed)]
    line = clean(reply)
    flat = ' '.join
    if not line or typed.rstrip().startswith(line) or flat(_one(typed).split()).startswith(flat(line.split())):
        return None
    return ('ghost' if typed and line.startswith(typed) else 'replace'), lead + line


def command_word(word, names, hist):
    """What the command word being typed becomes: itself when it already runs, else the one the user runs
    most, else fish's first candidate. None: nothing by that name, maybe a request in plain words."""
    if word in names:
        return word
    used = Counter(w for c in hist if (w := c.split(None, 1)[0] if c.strip() else '').startswith(word))
    return used.most_common(1)[0][0] if used else next((n for n in names if n.startswith(word)), None)


def runs(word, hist):
    """The user can run it: on $PATH, or ran it before (that covers their functions and fish's builtins)."""
    return bool(shutil.which(word)) or any(c.split(None, 1)[0] == word for c in hist if c.strip())


def suggest(buffer, prompt, start='', retry=True):
    """The model's line for the buffer. Small models like to say the line back: then (or right away, with
    start) their answer is begun for them (a prefill), which leaves them nothing to do but continue it.
    retry=False: a line that is no command (plain words) is never continued, that only makes prose."""
    typed = buffer.lstrip()
    end = time.monotonic() + TIMEOUT
    if not start:
        got = decide(buffer, generate(prompt))
        if got or not typed.strip() or not retry:
            return got
        start = typed.rstrip()
    left = end - time.monotonic()
    if left < 2:
        return None
    more = generate(prompt, prefill=start, timeout=left)
    return decide(buffer, start + more.removeprefix(start))


def explain(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            with e:
                detail = str(json.loads(e.read()).get('error', ''))
        except Exception:
            detail = ''
        if e.code == 404 or 'not found' in detail:
            return f'model {model()} is not installed; {SETUP}'
        return f'ollama failed: {detail[:120] or e.code}'
    if isinstance(e, TimeoutError) or isinstance(getattr(e, 'reason', None), TimeoutError):
        return None
    if isinstance(e, OSError):
        return f'ollama is not responding; {SETUP}'
    return 'ollama sent an invalid reply; check OLLAMA_HOST'


def warm(cwd, hist):
    """While the user uses ctrl-space, every new prompt has ollama read this directory's part of the question
    ahead of time (and keeps the model loaded), so the key itself only pays for the line. Not on battery:
    there it only lets go of the GPU."""
    if on_battery():
        release()
    else:
        generate(build_prompt(cwd, '', recent(cwd, hist), warm=True, tgt=target()), predict=1)


def release():
    """A model the GPU still holds from before the laptop was unplugged would keep it awake for the rest of
    keep_alive: unload it, the next ctrl-space loads it on the CPU. (A CPU-only ollama never gets here.)"""
    with _open(base_url() + '/api/ps', timeout=2) as r:
        held = any(m.get('size_vram') and m.get('name') in (model(), f'{model()}:latest')
                   for m in json.load(r).get('models', []))
    if held:
        body = json.dumps({'model': model(), 'keep_alive': 0}).encode()
        _open(urllib.request.Request(base_url() + '/api/generate', body, {'Content-Type': 'application/json'}),
              timeout=5).close()


def _superseded(pid, my_id):
    """True when a newer space press bumped the request after this one was fired. The fish side writes
    its latest id to {pid}.aireq, so rapid spaces each start a helper but only the last runs the model."""
    try:
        return (common.run_dir() / f'{pid}.aireq').read_text().strip() not in ('', my_id)
    except OSError:
        return False


def main(sub, argv):
    """stdin: what the command word being typed can become (see names()). Prints `kind<TAB>url(line)`, or
    nothing. --notify 1 --pid P --id N answers the shell asynchronously (SIGUSR1) instead of on stdout,
    and only with a ghost (an extension of the line): a space press must never rewrite what the user is typing.
    --warm 1: only read the directory ahead (detached, prints nothing)."""
    a = dict(zip(argv[::2], argv[1::2]))
    cwd = common.dec(a.get('--cwd', '')) or os.getcwd()
    if a.get('--warm'):
        try:
            warm(cwd, history(a.get('--fish-data'), a.get('--real-name') or 'fish'))
        except (OSError, ValueError, http.client.HTTPException):
            pass
        return 0
    async_ = a.get('--notify') and a.get('--pid')
    if async_ and _superseded(a['--pid'], a.get('--id', '0')):
        return 0
    buffer = common.dec(a.get('--buffer', ''))
    typed = buffer.lstrip()
    piped = sys.stdin is not None and not sys.stdin.isatty()
    text = sys.stdin.buffer.read().decode('utf-8', 'replace') if piped else ''
    try:
        hist = history(a.get('--fish-data'), a.get('--real-name') or 'fish')
        cmds = recent(cwd, hist)
        tgt = target()
        hint = cmd_hint(typed, common.dec(a.get('--cmd-desc', '')), a.get('--cmd-custom') == '1')
        word = command_typed(typed) and command_word(typed.strip(), names(text, typed), hist)
        if word:
            got = suggest(buffer, build_prompt(cwd, word + ' ', cmds, habits(word + ' ', hist), tgt=tgt, cmd_hint=hint), start=word)
        else:
            mine = habits(typed, hist) if typed.strip() else usual(a.get('--fish-data'), a.get('--pid'), hist)
            got = suggest(buffer, build_prompt(cwd, typed, cmds, mine, tgt=tgt, cmd_hint=hint),
                          retry=bool(typed.split()) and runs(typed.split()[0], hist))
    except (OSError, ValueError, http.client.HTTPException) as e:
        got = None if async_ else (msg := explain(e)) and ('error', msg)
    if got and async_:
        if got[0] == 'ghost':
            common.notify(a['--pid'], 'ghost', a.get('--id', '0'), common.sanitize(got[1]))
    elif got:
        print(f'{got[0]}\t{common.enc(common.sanitize(got[1]))}')
    return 0


def doctor_lines():
    m = model()
    power = 'power: battery (the AI runs on the CPU only)' if on_battery() \
        else 'power: plugged in (the AI can use the GPU)'
    try:
        url = base_url()
        with _open(url + '/api/tags', timeout=2) as r:
            names = {x.get('name') for x in json.load(r).get('models', [])}
    except Exception:
        return [f'ollama: not responding ({os.environ.get("OLLAMA_HOST") or "127.0.0.1:11434"}); {SETUP}', power]
    have = m in names or f'{m}:latest' in names
    return [f'ollama: responding at {url}',
            f'AI model: {m} ' + ('installed' if have else f'missing; {SETUP}'), power]
