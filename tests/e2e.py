#!/usr/bin/env python3
"""fishsense end to end: a real interactive fish (your config + the plugin) driven by a private tmux.

    python3 tests/e2e.py [-k text] [--keep] [--list]

Each scenario builds its own world under a temp dir (HOME, XDG_*): copies your ~/.config/fish
(config.fish, conf.d, functions, completions) and starship.toml, installs the repo the way fisher does and
seeds a test history and context log. Your real config and history are only read.
"""
import argparse
import itertools
import json
import os
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
FIXTURES = TESTS / 'e2e_fixtures'
sys.path.insert(0, str(REPO / 'conf.d' / 'fishsense'))
import common

USER_CONFIG = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
SOCK = f'fishsense-e2e-{os.getpid()}'
W, H = 120, 40
GHOST_FG = {'38;2;255;0;175', '38;5;199'}
NOW = int(time.time())
DAY = 86400
MODEL = 'qwen2.5-coder:3b'
PASS_ENV = ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES', 'USER', 'LOGNAME', 'TZ')

E2E_CONF = r'''status is-interactive; or exit
set -g fish_color_autosuggestion ff00af
set -q fish_private_mode; or set -g fishsense_ai_autospace 0
set -q fish_private_mode; or set -g fishsense_ai_model qwen2.5-coder:3b
function __e2e_prompt --on-event fish_prompt
    set -q __e2e_n; or set -g __e2e_n 0
    set __e2e_n (math $__e2e_n + 1)
    echo $__e2e_n >$XDG_RUNTIME_DIR/e2e/$fish_pid.prompt
end
function __e2e_dump --on-signal SIGUSR2
    begin
        commandline -C
        commandline --paging-mode; and echo 1; or echo 0
        echo $fish_history
        if set -q E2E_PROBE
            complete -c $E2E_PROBE | count
        else
            echo 0
        end
        commandline
        echo '<e2e-end>'
    end >$XDG_RUNTIME_DIR/e2e/$fish_pid.state
end
'''

FAKE_BIN = '#!/bin/sh\nprintf \'%s\\n\' "${0##*/} $*" >>"${0%/*}/../marks"\n'
FAKE_CMDS = ('fakeapp', 'faketrace', 'fsbuild', 'fsdeploy', 'fsdiff', 'zqfoo', 'zqfob')

HISTORY = [
    'rm old-notes.txt', 'rm build.log', 'git status', 'git log --oneline', 'ls', 'fakeapp launch',
    'fsbuild', 'fsdeploy --prod', 'echo del-keep-e2e', 'echo paste-seeded-e2e', 'echo vi-seeded-e2e',
    'fsdiff --stat', 'faketrace record', 'echo del-me-e2e',
]


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def wait_until(pred, what, timeout=5.0, interval=0.03):
    """Poll pred until truthy and return it; `what` (str or callable) explains the timeout."""
    end = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() > end:
            raise Fail(f'after {timeout:g} s: {what() if callable(what) else what}')
        time.sleep(interval)


def tmux(*args, check=True):
    return subprocess.run(['tmux', '-L', SOCK, '-f', '/dev/null', *args],
                          capture_output=True, text=True, check=check).stdout


def dead_host():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return f'127.0.0.1:{s.getsockname()[1]}'


def _fg(fg, params):
    """Foreground colour after one SGR sequence (None = default)."""
    ps = params.replace(':', ';').split(';') if params else ['0']
    i = 0
    while i < len(ps):
        p = ps[i]
        if p in ('', '0', '39'):
            fg = None
        elif p == '38' and i + 1 < len(ps):
            n = 5 if ps[i + 1] == '2' else 3
            fg, i = ';'.join(ps[i:i + n]), i + n - 1
        elif p.isdigit() and (30 <= int(p) <= 37 or 90 <= int(p) <= 97):
            fg = p
        i += 1
    return fg


def ghost_text(raw):
    """Characters drawn in the autosuggestion colour, wrapped rows joined."""
    raw = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', raw)
    out, fg, pos = [], None, 0
    for m in re.finditer(r'\x1b\[([0-9;:]*)m', raw):
        if fg in GHOST_FG:
            out.append(raw[pos:m.start()])
        fg, pos = _fg(fg, m.group(1)), m.end()
    if fg in GHOST_FG:
        out.append(raw[pos:])
    return ''.join(out).replace('\n', '')


class FakeOllama:
    """Just enough of ollama's HTTP API: canned replies chosen by a needle found in the request
    (needle, text[, seconds to wait before answering])."""

    def __init__(self, replies):
        self.replies, self.requests = replies, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, obj, code=200):
                data = (json.dumps(obj) + '\n').encode()
                try:
                    self.send_response(code)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def do_GET(self):
                if self.path.startswith('/api/tags'):
                    return self.reply({'models': [{'name': MODEL, 'model': MODEL}]})
                if self.path.startswith('/api/version'):
                    return self.reply({'version': '0.0.0-e2e'})
                self.reply({'error': 'not found'}, 404)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode()
                body = json.loads(raw or '{}')
                outer.requests.append((self.path, body))
                text, delay = next(((r[1], r[2:]) for r in outer.replies if r[0] in raw), ('echo no-reply-e2e', ()))
                time.sleep(delay[0] if delay else 0)
                if self.path == '/api/chat':
                    return self.reply({'model': body.get('model'), 'done': True,
                                       'message': {'role': 'assistant', 'content': text}})
                self.reply({'error': 'not found'}, 404)

        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.host = f'127.0.0.1:{self.srv.server_port}'

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Env:
    """One isolated world: HOME/XDG dirs, the user's fish config, the plugin, fixtures."""

    def __init__(self, plugin=True):
        r = self.root = Path(tempfile.mkdtemp(prefix='fishsense-e2e-'))
        self.home, self.config, self.data, self.cache, self.run, self.bin = (
            r / n for n in ('home', 'config', 'data', 'cache', 'run', 'bin'))
        self.fish_conf = self.config / 'fish'
        self.fish_data = self.data / 'fish'
        self.real_hist = self.fish_data / 'fish_history'
        self.log = self.data / 'fishsense' / 'log.tsv'
        self.proj, self.other, self.work, self.repo, self.pager = (
            self.home / n for n in ('Projects/app', 'other', 'work', 'repo', 'pager'))
        self.shells, self.closers, self.extra = [], [], {'OLLAMA_HOST': dead_host()}
        for d in (self.fish_conf, self.fish_data, self.cache / 'fish' / 'generated_completions', self.run / 'e2e',
                  self.bin, self.proj, self.other, self.work, self.repo, self.pager):
            d.mkdir(parents=True, exist_ok=True)
        self.run.chmod(0o700)
        self._copy_user_config()
        if plugin:
            self._install()
        (self.fish_conf / 'conf.d' / 'zz-e2e.fish').write_text(E2E_CONF)
        self._fixture()

    def _copy_user_config(self):
        src = USER_CONFIG / 'fish'
        skip = shutil.ignore_patterns('fishsense*', '__fishsense*', '__pycache__')
        if (src / 'config.fish').is_file():
            shutil.copy(src / 'config.fish', self.fish_conf)
        for d in ('conf.d', 'functions', 'completions'):
            if (src / d).is_dir():
                shutil.copytree(src / d, self.fish_conf / d, ignore=skip, dirs_exist_ok=True,
                                ignore_dangling_symlinks=True)
            (self.fish_conf / d).mkdir(exist_ok=True)
        if (USER_CONFIG / 'starship.toml').is_file():
            shutil.copy(USER_CONFIG / 'starship.toml', self.config / 'starship.toml')
        if (Path.home() / 'Scripts').is_dir():
            (self.home / 'Scripts').symlink_to(Path.home() / 'Scripts')

    def _install(self):
        for d in ('functions', 'conf.d', 'completions'):
            if (REPO / d).is_dir():
                shutil.copytree(REPO / d, self.fish_conf / d, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns('__pycache__'))

    def _fixture(self):
        for name in FAKE_CMDS:
            p = self.bin / name
            p.write_text(FAKE_BIN)
            p.chmod(0o755)
        for f in ('pager/pagerA-one.txt', 'pager/pagerA-two.txt', 'work/target.txt'):
            (self.home / f).write_text('e2e\n')
        subprocess.run(['git', 'init', '-q', str(self.repo)], env=self.env(), check=True)
        self.real_hist.write_text(''.join(
            f'- cmd: {common.escape_hist(c)}\n  when: {NOW - (len(HISTORY) - i) * 600}\n'
            for i, c in enumerate(HISTORY)))
        common.write_log(self._log_fixture(), self.log)
        seed_tldr(self.cache / 'fishsense' / 'tldr')
        self._check_fakes()

    def _check_fakes(self):
        out = subprocess.run(['fish', '-c', 'for c in $argv; command -s $c; or echo missing:$c; end', *FAKE_CMDS],
                             env=self.env(), capture_output=True, text=True, timeout=30).stdout.split()
        bad = [o for o in out if Path(o).parent != self.bin]
        if bad:
            shutil.rmtree(self.root, ignore_errors=True)
            raise SystemExit(f'aborted: the test commands do not resolve to {self.bin}: {bad}')

    def _log_fixture(self):
        recs, t = [], [NOW - 3 * DAY]

        def add(pid, cwd, *cmds):
            for c in cmds:
                t[0] += 60
                recs.append(common.LogRec(t[0], pid, 0, str(cwd), c))

        for _ in range(3):
            add(4100001, self.repo, 'git status', 'rm old-notes.txt', 'git log --oneline', 'rm build.log')
        for _ in range(6):
            add(4100002, self.proj, 'fakeapp launch')
        for _ in range(4):
            add(4100003, self.work, 'fsbuild', 'fsdeploy --prod')
        t[0] = NOW - DAY
        for _ in range(4):
            add(4100004, self.work, 'ls', 'fsdiff --stat')
        for _ in range(6):
            add(4100005, self.other, 'faketrace record')
        return recs

    def env(self, **extra):
        e = {k: v for k in PASS_ENV if (v := os.environ.get(k))}
        e.setdefault('LANG', 'C.UTF-8')
        e.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.config), XDG_DATA_HOME=str(self.data),
                 XDG_CACHE_HOME=str(self.cache), XDG_RUNTIME_DIR=str(self.run), TERM='tmux-256color',
                 PATH=f'{self.bin}:{os.environ.get("PATH", "/usr/bin")}')
        if (self.config / 'starship.toml').is_file():
            e['STARSHIP_CONFIG'] = str(self.config / 'starship.toml')
        e.update(self.extra)
        e.update(extra)
        return e

    def add_config(self, text):
        """Extra lines at the end of config.fish (it runs after conf.d, like the user's does)."""
        with open(self.fish_conf / 'config.fish', 'a') as f:
            f.write('\n' + text + '\n')

    def shell(self, cwd=None, args=(), **extra):
        return Shell(self, cwd or self.home, args, extra)

    def sibling(self, plugin):
        """Another world cleaned up together with this one (e.g. a plugin-less baseline)."""
        e = Env(plugin)
        self.closers.append(lambda keep: e.close(keep))
        return e

    def fake_ollama(self, replies):
        f = FakeOllama(replies)
        self.closers.append(lambda keep: f.close())
        return f

    def real_history(self):
        return [i.cmd for i in common.read_fish_history(self.real_hist)]

    def log_recs(self):
        return common.read_log(self.log)

    def log_cmds(self):
        return [r.cmd for r in self.log_recs()]

    def marks(self):
        try:
            return (self.root / 'marks').read_text().splitlines()
        except OSError:
            return []

    def plugin_files(self):
        """Everything fishsense may write, with mtimes."""
        found = set(self.fish_data.glob('fishsense_*'))
        for d in (self.data / 'fishsense', self.cache / 'fishsense', self.run / 'fishsense'):
            if d.exists():
                found.update(d.rglob('*'))
        return {str(p): p.stat().st_mtime_ns for p in found if p.exists()}

    def screens(self):
        return '\n'.join(f'--- {s.name} (fish {s.pid}) ---\n{s.screen(check=False)}' for s in self.shells)

    def close(self, keep):
        for c in self.closers:
            c(keep)
        if keep:
            print(f'  kept: {self.root}  (tmux -L {SOCK} attach -t {", ".join(s.name for s in self.shells)})')
            return
        for s in self.shells:
            s.kill()
        self._kill_strays()
        shutil.rmtree(self.root, ignore_errors=True)

    def _kill_strays(self):
        """Background helpers outlive their shell; anything whose environment points into root goes."""
        mark = f'={self.root}/'.encode()
        for p in Path('/proc').iterdir():
            try:
                if p.name.isdigit() and mark in (p / 'environ').read_bytes():
                    os.kill(int(p.name), signal.SIGKILL)
            except OSError:
                pass


def seed_tldr(dest):
    """Fixture pages in params.py's cache layout (<lang>/<platform>/<cmd>.md), index built by params itself."""
    import params
    shutil.copytree(FIXTURES / 'tldr', dest, dirs_exist_ok=True)
    params.build_index(dest)


_ids = itertools.count(1)


class Shell:
    """An interactive fish in its own tmux session."""

    def __init__(self, env, cwd, args, extra):
        self.env, self.name = env, f's{next(_ids)}'
        cmd = ['env', '-i', *(f'{k}={v}' for k, v in env.env(**extra).items()), 'fish', *args]
        tmux('new-session', '-d', '-s', self.name, '-x', str(W), '-y', str(H), '-c', str(cwd), '--', *cmd)
        self.pid = int(tmux('display-message', '-p', '-t', self.name, '#{pane_pid}'))
        env.shells.append(self)
        self.wait_prompt(1, timeout=15)

    def send(self, *keys):
        tmux('send-keys', '-t', self.name, *keys)

    def type(self, text):
        tmux('send-keys', '-t', self.name, '-l', '--', text)

    def paste(self, text):
        tmux('set-buffer', '-b', 'e2e', '--', text)
        tmux('paste-buffer', '-p', '-r', '-d', '-b', 'e2e', '-t', self.name)

    def run(self, cmd, timeout=10):
        n = self.prompts()
        if cmd:
            self.type(cmd)
        self.send('Enter')
        self.wait_prompt(n + 1, timeout)

    def screen(self, color=False, check=True):
        return tmux('capture-pane', '-p', '-J', *(['-e'] if color else []), '-t', self.name, check=check)

    def ghost(self):
        return ghost_text(self.screen(color=True))

    def prompts(self):
        try:
            return int((self.env.run / 'e2e' / f'{self.pid}.prompt').read_text())
        except (OSError, ValueError):
            return 0

    def state(self):
        """Exact reader state: cursor, paging, active history session, buffer."""
        f = self.env.run / 'e2e' / f'{self.pid}.state'
        f.unlink(missing_ok=True)
        end = '<e2e-end>\n'
        for _ in range(6):
            os.kill(self.pid, signal.SIGUSR2)
            if text := self.settle(lambda: f.exists() and (t := f.read_text()).endswith(end) and t, 0.5):
                break
        else:
            raise Fail('fish did not answer SIGUSR2 (is it busy?)')
        cursor, paging, session, probe, buf = text.removesuffix(end).split('\n', 4)
        return {'cursor': int(cursor), 'paging': paging == '1', 'session': session, 'probe': int(probe),
                'buffer': buf[:-1]}

    def buffer(self):
        return self.state()['buffer']

    def wait(self, pred, what, timeout=5.0):
        return wait_until(pred, what, timeout)

    def wait_prompt(self, n, timeout=10):
        wait_until(lambda: self.prompts() >= n, f'prompt #{n} of {self.name} never showed up', timeout, 0.003)

    def ranked(self):
        """Wait for the shadow session: the steady state, the one where history mix-ups can happen."""
        self.wait(lambda: self.state()['session'].startswith('fishsense_'), 'the shadow history never became active')

    def wait_ghost(self, want, timeout=5.0):
        self.wait(lambda: self.ghost() == want, lambda: f'ghost {want!r}, showing {self.ghost()!r}', timeout)

    def wait_buffer(self, want, timeout=5.0):
        self.wait(lambda: self.buffer() == want, lambda: f'buffer {want!r}, got {self.buffer()!r}', timeout)

    def settle(self, pred, timeout):
        """Like wait, but only gives something a chance to happen."""
        try:
            return self.wait(pred, '', timeout)
        except Fail:
            return None

    def alive(self):
        try:
            os.kill(self.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def kill(self):
        tmux('kill-session', '-t', self.name, check=False)


SCENARIOS = []


def scenario(fn):
    SCENARIOS.append(fn)
    return fn


@scenario
def ranked_ghost_cwd(env):
    """The ghost comes from the cwd ranking, not from the newest command."""
    sh = env.shell(env.proj)
    sh.type('fake')
    sh.wait_ghost('app launch')
    sh.send('C-c')
    sh.run(f'cd {env.other}')
    sh.type('fake')
    sh.wait_ghost('trace record')


@scenario
def transition(env):
    """After fsbuild the ghost offers what always follows it."""
    sh = env.shell(env.work)
    sh.type('fsd')
    sh.wait_ghost('iff --stat')
    sh.send('C-c')
    sh.run('fsbuild')
    sh.type('fsd')
    sh.wait_ghost('eploy --prod')


@scenario
def executed_lands_in_real(env):
    """What runs lands in the real history; synthetic items (structures, injected ghosts) never do."""
    seeded = env.real_history()
    sh = env.shell(env.proj)
    shadow = env.fish_data / f'fishsense_{sh.pid}_history'
    sh.wait(shadow.exists, 'this shell never created its shadow history')
    check(sh.state()['session'] == shadow.name.removesuffix('_history'), 'while editing, the active session is not the shadow')
    sh.type('fake')
    sh.wait_ghost('app launch')
    sh.send('Right')
    sh.wait_buffer('fakeapp launch')
    sh.run('')
    sh.type('tar ')
    sh.settle(lambda: '‹' in sh.ghost(), 4)
    sh.send('C-c')
    sh.run('echo real-e2e')
    items = sh.wait(lambda: (h := env.real_history())[-2:] == ['fakeapp launch', 'echo real-e2e'] and h,
                    lambda: f'real history ends with {env.real_history()[-3:]}')
    extra = set(items) - set(seeded) - {'echo real-e2e'}
    check(not extra, f'synthetic items in the real history: {sorted(extra)}')
    check(env.marks()[-1:] == ['fakeapp launch'], f'fakeapp launch did not run: {env.marks()}')


@scenario
def leading_space(env):
    """A command with a leading space goes to neither the real history nor the log."""
    sh = env.shell(env.work)
    sh.ranked()
    sh.run(' echo secret-e2e')
    sh.run('echo after-e2e')
    sh.wait(lambda: 'echo after-e2e' in env.real_history(), 'echo after-e2e did not reach the real history')
    sh.wait(lambda: 'echo after-e2e' in env.log_cmds(), 'echo after-e2e did not reach the log')
    check(not any('secret-e2e' in c for c in env.real_history()), 'the leading-space command is in the history')
    check(not any('secret-e2e' in c for c in env.log_cmds()), 'the leading-space command is in the log')


@scenario
def up_arrow_chronological(env):
    """↑ walks the real history in order, even when the ranking prefers something else."""
    sh = env.shell(env.proj)
    sh.ranked()
    sh.run('echo up-1-e2e')
    sh.run('echo up-2-e2e')
    for want in ('echo up-2-e2e', 'echo up-1-e2e', 'echo del-me-e2e'):
        sh.send('Up')
        sh.wait_buffer(want)
    sh.send('C-c')
    sh.wait_buffer('')
    sh.type('fake')
    sh.wait_ghost('app launch')
    sh.send('Up')
    sh.wait_buffer('faketrace record')
    sh.send('Escape')
    sh.wait_buffer('fake')
    sh.wait_ghost('app launch')


@scenario
def fzf_ctrl_r(env):
    """fzf.fish ctrl-r lists the real history, newest first."""
    sh = env.shell(env.proj)
    sh.run('echo fzf-newest-e2e')
    time.sleep(1.2)
    sh.send('C-r')
    rows = sh.wait(lambda: re.findall(r'\d\d-\d\d \d\d:\d\d:\d\d │ (.*?)\s*│?\s*$', sh.screen(), re.M),
                   'fzf did not show the history list')
    check(rows[0] == 'echo fzf-newest-e2e', f'first fzf row: {rows[0]!r}')
    real = set(env.real_history())
    check(all(r in real for r in rows), f'fzf lists things that are not in the real history: '
          f'{[r for r in rows if r not in real]}')
    sh.send('Escape')
    sh.wait(lambda: 'History>' not in sh.screen(), 'fzf did not close')
    sh.wait_buffer('')


@scenario
def shift_delete(env):
    """shift-delete removes the visible suggestion from the real history too."""
    sh = env.shell(env.work)
    sh.type('echo del-')
    shown = sh.wait(lambda: (g := sh.ghost()) in ('me-e2e', 'keep-e2e') and g,
                    lambda: f'no ghost for echo del- ({sh.ghost()!r})')
    gone, kept = 'echo del-' + shown, 'echo del-' + ({'me-e2e', 'keep-e2e'} - {shown}).pop()
    sh.send('S-DC')
    sh.wait(lambda: gone not in env.real_history(), f'{gone!r} is still in the real history')
    check(kept in env.real_history(), f'{kept!r} disappeared too')
    sh.wait(lambda: sh.ghost() != shown, 'the deleted suggestion is still showing')
    check(sh.buffer() == 'echo del-', f'shift-delete changed the buffer: {sh.buffer()!r}')


@scenario
def pager_tab_enter(env):
    """Tab Tab Enter in the pager accepts the option without running anything."""
    sh = env.shell(env.pager)
    sh.ranked()
    n = sh.prompts()
    sh.type('echo pagerA-')
    sh.send('Tab')
    sh.wait(lambda: 'pagerA-two.txt' in sh.screen(), 'Tab did not open the pager')
    sh.send('Tab')
    sh.wait(lambda: 'echo pagerA-one.txt' in sh.screen(), 'the second Tab did not pick the first option')
    sh.send('Enter')
    sh.wait(lambda: 'pagerA-two.txt' not in sh.screen(), 'Enter did not close the pager')
    time.sleep(0.4)
    check(sh.prompts() == n, 'Enter in the pager ran the line')
    st = sh.state()
    check(st['buffer'].startswith('echo pagerA-one.txt') and not st['paging'], f'state after Enter: {st}')
    check(not any('pagerA' in c for c in env.real_history()), 'the line reached the history')


@scenario
def abbr_on_space(env):
    """Abbreviations still expand on space."""
    sh = env.shell(env.work)
    sh.ranked()
    n = sh.prompts()
    sh.type('gs ')
    sh.wait_buffer('git status ')
    check(sh.prompts() == n, 'something ran')


@scenario
def next_param(env):
    """After a space, the ghost offers the next parameter (command without completions)."""
    sh = env.shell(env.work)
    sh.type('waifu2x-ncnn-vulkan ')
    sh.wait(lambda: '-i' in sh.ghost(), lambda: f'the ghost does not offer -i: {sh.ghost()!r}', timeout=8)
    check(sh.buffer() == 'waifu2x-ncnn-vulkan ', f'the buffer changed: {sh.buffer()!r}')


@scenario
def generated_completions(env):
    """Tab completes flags and values generated from --help."""
    sh = env.shell(env.work, E2E_PROBE='waifu2x-ncnn-vulkan')
    sh.type('waifu2x-ncnn-vulkan ')
    comp = env.cache / 'fishsense' / 'completions' / 'waifu2x-ncnn-vulkan.fish'
    sh.wait(comp.exists, f'{comp} was not generated', timeout=8)
    sh.wait(lambda: sh.state()['probe'] > 0, 'fish did not load the generated completions')
    sh.type('-')
    sh.send('Tab')
    sh.wait(lambda: re.search(r'-s\b.*upscale', sh.screen()), 'the pager does not show -s with its description')
    sh.send('C-c')
    sh.wait_buffer('')
    sh.type('waifu2x-ncnn-vulkan -s ')
    sh.send('Tab')
    sh.wait(lambda: all(re.search(rf'(^|\s){v}(\s|$)', sh.screen(), re.M) for v in ('16', '32')),
            'Tab after -s does not offer the values 16/32')


@scenario
def placeholder_trim(env):
    """Accepting a ghost with ‹…› inserts only up to the placeholder."""
    sh = env.shell(env.work)
    sh.type('tar ')
    g = sh.wait(lambda: '‹' in (g := sh.ghost()) and g, lambda: f'no ghost with ‹…›: {sh.ghost()!r}', 8)
    want = 'tar ' + g[:g.index('‹')]
    sh.send('Right')
    sh.wait_buffer(want)
    check(sh.state()['cursor'] == len(want), 'the cursor did not stop before the placeholder')


@scenario
def tldr_structure(env):
    """The structure comes from the English tldr page."""
    sh = env.shell(env.work)
    sh.type('tar ')
    sh.wait_ghost('-c -f ‹path/to/target.tar› ‹path/to/file1 path/to/file2 ...›', timeout=8)


@scenario
def autocorrect_fix(env):
    """gti status gets corrected to git status and runs, silently (no notice)."""
    check(not shutil.which('gti', path=env.env()['PATH']), 'gti exists on this system; the scenario does not apply')
    sh = env.shell(env.repo)
    sh.ranked()
    sh.run('gti status')
    sh.wait(lambda: env.real_history()[-1:] == ['git status'], 'git status is not last in the real history')
    check('corrected from' not in sh.screen(), 'the correction notice showed up')
    check('gti status' not in env.real_history(), 'the mistyped line stayed in the history')
    last = sh.wait(lambda: (r := env.log_recs()) and r[-1].pid == sh.pid and r[-1],
                   lambda: f'last log record: {env.log_recs()[-1:]}')
    check((last.cmd, last.status) == ('git status', 0), f'last log record: {last}')


@scenario
def autocorrect_command_and_sub(env):
    """gti stauts: the command and its subcommand get fixed in one go."""
    sh = env.shell(env.repo)
    sh.ranked()
    sh.run('gti stauts')
    sh.wait(lambda: env.real_history()[-1:] == ['git status'], 'did not fix both words')


@scenario
def destructive_ask(env):
    """A destructive correction asks; n runs the original."""
    sh = env.shell(env.work)
    sh.ranked()
    n = sh.prompts()
    sh.type('mr target.txt')
    sh.send('Enter')
    sh.wait(lambda: 'did you mean: rm target.txt? [Y/n]' in sh.screen(), 'did not ask before rm')
    sh.send('n')
    if not sh.settle(lambda: sh.prompts() > n, 1):
        sh.send('Enter')
    sh.wait_prompt(n + 1)
    check((env.work / 'target.txt').exists(), 'target.txt was deleted')
    sh.wait(lambda: 'mr target.txt' in env.log_cmds(), 'the original line (mr target.txt) did not run')


@scenario
def destructive_ask_yes(env):
    """Enter accepts the correction and the fixed line reaches the real history."""
    sh = env.shell(env.work)
    sh.ranked()
    n = sh.prompts()
    sh.type('mr target.txt')
    sh.send('Enter')
    sh.wait(lambda: 'did you mean: rm target.txt? [Y/n]' in sh.screen(), 'did not ask before rm')
    sh.send('Enter')
    sh.wait_prompt(n + 1)
    check(not (env.work / 'target.txt').exists(), 'rm target.txt did not run')
    sh.run('echo after-ask-e2e')
    sh.wait(lambda: env.real_history()[-2:] == ['rm target.txt', 'echo after-ask-e2e'],
            lambda: f'real history ends with {env.real_history()[-3:]}')


@scenario
def ambiguous_unchanged(env):
    """A typo with two equally good candidates is left alone."""
    sh = env.shell(env.work)
    sh.ranked()
    sh.run('zqfoa x')
    screen = sh.screen()
    check('did you mean' not in screen and 'corrected' not in screen, 'corrected something ambiguous')
    check(env.marks() == [], f'a candidate ran: {env.marks()}')
    sh.wait(lambda: env.log_cmds()[-1:] == ['zqfoa x'], 'the original line is not in the log')


@scenario
def ai_ctrl_space(env):
    """ctrl-space: if the AI extends the line it is a ghost; otherwise it replaces it (ctrl-z undoes)."""
    fake = env.fake_ollama([('list all e2e', 'ls -la'), ('hello-e2e', 'echo hello-e2e world-ai')])
    sh = env.shell(env.work, OLLAMA_HOST=fake.host)
    sh.ranked()
    sh.type('echo hello-e2e')
    sh.send('C-Space')
    sh.wait_ghost(' world-ai', timeout=8)
    check(sh.buffer() == 'echo hello-e2e', f'the AI ghost touched the buffer: {sh.buffer()!r}')
    sh.send('C-c')
    sh.wait_buffer('')
    sh.type('list all e2e')
    sh.send('C-Space')
    sh.wait_buffer('ls -la', timeout=8)
    sh.send('C-z')
    sh.wait_buffer('list all e2e')
    sh.send('C-c')
    sh.run('echo ai-done-e2e')
    sh.wait(lambda: 'echo ai-done-e2e' in env.real_history(), 'echo ai-done-e2e did not reach the real history')
    check(not any('world-ai' in c for c in env.real_history()), 'the AI ghost reached the real history')
    body = fake.requests[0][1]
    check(body.get('model') == MODEL and body.get('keep_alive') == '1h', f'ollama request: {body}')


@scenario
def ai_ctrl_c(env):
    """ctrl-c while the AI thinks: stops the wait and keeps the line. Nothing on screen while it thinks."""
    fake = env.fake_ollama([('slow-e2e', 'echo late-e2e', 15)])
    sh = env.shell(env.work, OLLAMA_HOST=fake.host)
    sh.type('echo slow-e2e')
    sh.wait_buffer('echo slow-e2e')
    before = sh.screen()
    sh.send('C-Space')
    sh.wait(lambda: fake.requests, 'the request never reached ollama')
    time.sleep(0.3)
    check(sh.screen() == before, f'something showed up while the AI thinks:\n{sh.screen()}')
    sh.send('C-c')
    time.sleep(0.3)
    check(sh.buffer() == 'echo slow-e2e', f'ctrl-c changed the buffer: {sh.buffer()!r}')
    sh.send('C-c')
    sh.wait_buffer('')
    n = sh.prompts()
    sh.type('for i in (seq 50); sleep 1; end')
    sh.send('Enter')
    time.sleep(0.5)
    sh.send('C-c')
    sh.wait_prompt(n + 1, timeout=3)


@scenario
def ai_down_hint(env):
    """Without ollama, ctrl-space only leaves a fishsense setup-ai hint (and no … on screen)."""
    sh = env.shell(env.work)
    sh.type('echo x-e2e')
    sh.send('C-Space')
    sh.wait(lambda: 'fishsense setup-ai' in sh.screen(), 'the fishsense setup-ai hint never showed up', 8)
    check(sh.buffer() == 'echo x-e2e', f'the buffer changed: {sh.buffer()!r}')
    check('…' not in sh.screen(), 'the … hint stayed on the row above')


@scenario
def ai_nothing_quiet(env):
    """When the AI only says the line back, ctrl-space leaves no trace: no message, same line."""
    fake = env.fake_ollama([('echo same-e2e', 'echo same-e2e')])
    sh = env.shell(env.work, OLLAMA_HOST=fake.host)
    sh.type('echo same-e2e')
    sh.wait_buffer('echo same-e2e')
    before = sh.screen()
    sh.send('C-Space')
    sh.wait(lambda: len(fake.requests) == 2, lambda: f'expected the answer and its retry: {len(fake.requests)}', 8)
    time.sleep(0.5)
    check(sh.screen() == before, f'ctrl-space left something on screen:\n{sh.screen()}')
    check(sh.buffer() == 'echo same-e2e', f'the buffer changed: {sh.buffer()!r}')


@scenario
def ai_warms_after_use(env):
    """Once ctrl-space was used, each new prompt has ollama read the directory ahead (a 1-token request).
    On battery there is no warm-up at all, so the last check flips."""
    import ai
    battery = ai.on_battery()
    fake = env.fake_ollama([('warm-e2e', 'echo warm-e2e done')])
    sh = env.shell(env.work, OLLAMA_HOST=fake.host)
    sh.run('true never-used-e2e')
    time.sleep(1)
    check(fake.requests == [], f'it warmed up before ctrl-space was ever used: {fake.requests}')
    sh.type('echo warm-e2e')
    sh.send('C-Space')
    sh.wait(lambda: fake.requests, 'ctrl-space never reached ollama', 8)
    sh.send('C-c')
    n = len(fake.requests)
    sh.run('true used-e2e')
    if battery:
        time.sleep(1.5)
        check(len(fake.requests) == n, f'a warm-up on battery: {fake.requests[n:]}')
        return
    sh.wait(lambda: len(fake.requests) > n, 'no warm-up after the next command', 8)
    body = fake.requests[-1][1]
    check(body.get('options', {}).get('num_predict') == 1 and 'line:' not in json.dumps(body['messages'][-1]),
          f'warm-up request: {body}')


@scenario
def ai_unknown_command(env):
    """If the AI completes with a command that does not exist, fish would not show it as a ghost: it goes into the buffer."""
    fake = env.fake_ollama([('zqnot', 'zqnotacmd --zz-e2e')])
    sh = env.shell(env.work, OLLAMA_HOST=fake.host)
    sh.ranked()
    sh.type('zqnot')
    sh.send('C-Space')
    sh.wait_buffer('zqnotacmd --zz-e2e', timeout=8)


@scenario
def ai_autospace(env):
    """With fishsense_ai_autospace on and plugged in, a space asks the AI in the background: a concrete
    ghost appears on its own, no ctrl-space. It only ever extends the line (a ghost), never replaces it."""
    import ai
    if ai.on_battery():
        return
    fake = env.fake_ollama([('waifu2x -i a-e2e.png', 'waifu2x -i a-e2e.png -o a-e2e_2x.png -s 2')])
    sh = env.shell(env.work, OLLAMA_HOST=fake.host)
    sh.ranked()
    sh.run('set -g fishsense_ai_autospace 1')
    (env.work / 'a-e2e.png').write_text('')
    sh.type('waifu2x -i a-e2e.png ')
    sh.wait_ghost('-o a-e2e_2x.png -s 2', timeout=10)
    check(sh.buffer() == 'waifu2x -i a-e2e.png ', f'the auto-space ghost touched the buffer: {sh.buffer()!r}')
    sh.send('C-c')


@scenario
def vi_mode(env):
    """Vi mode: ghost in insert; Enter (insert and normal) goes to the real history."""
    sh = env.shell(env.work)
    sh.ranked()
    sh.run('fish_vi_key_bindings')
    sh.type('echo vi-')
    sh.wait_ghost('seeded-e2e')
    sh.type('run-e2e')
    sh.run('')
    sh.wait(lambda: 'echo vi-run-e2e' in env.real_history(), 'Enter in insert mode did not reach the real history')
    n = sh.prompts()
    sh.type('echo vi-normal-e2e')
    sh.send('Escape')
    time.sleep(0.2)
    sh.send('Enter')
    sh.wait_prompt(n + 1)
    sh.wait(lambda: 'echo vi-normal-e2e' in env.real_history(), 'Enter in normal mode did not reach the real history')


@scenario
def multiline_paste(env):
    """Pasting several lines over a ghost inserts the text as is and runs nothing."""
    sh = env.shell(env.work)
    sh.ranked()
    sh.type('echo pa')
    sh.wait_ghost('ste-seeded-e2e')
    n = sh.prompts()
    sh.paste('rt-one\necho part-two')
    sh.wait_buffer('echo part-one\necho part-two')
    check(sh.prompts() == n, 'the paste ran something')
    sh.send('Enter')
    sh.wait_prompt(n + 1)
    lines = sh.screen().splitlines()
    check('part-one' in lines and 'part-two' in lines, 'the two lines did not run')
    sh.wait(lambda: 'echo part-one\necho part-two' in env.real_history(), 'the multi-line command did not reach the real history')


@scenario
def signal_storm(env):
    """A storm of SIGUSR1 with junk results, ctrl-l and a resize does not take fish down."""
    sh = env.shell(env.work)
    sh.ranked()
    sh.type('echo ali')
    res = env.run / 'fishsense' / f'{sh.pid}.result'
    junk = ['', 'garbage without tabs\n', f'ghost\te2e-stale\t{common.enc("echo alivejunk-e2e")}\n',
            f'ghost\te2e-stale\t{common.enc("echo injected-e2e")}\n', 'ranked\te2e\t\n',
            f'compgen\te2e\t{common.enc(str(env.root / "no-such.fish"))}\n']
    for i in range(200):
        if i % 10 == 0:
            common.write_atomic(res, junk[i // 10 % len(junk)])
        try:
            os.kill(sh.pid, signal.SIGUSR1)
        except ProcessLookupError:
            raise Fail(f'fish died at SIGUSR1 #{i + 1}') from None
        time.sleep(0.002)
    sh.send('C-l')
    tmux('resize-window', '-t', sh.name, '-x', '80', '-y', '24')
    time.sleep(0.2)
    tmux('resize-window', '-t', sh.name, '-x', str(W), '-y', str(H))
    sh.type('ve-e2e')
    sh.wait_buffer('echo alive-e2e')
    check('junk' not in sh.ghost() and 'injected' not in sh.ghost(), f'a junk ghost slipped through: {sh.ghost()!r}')
    sh.run('')
    check('alive-e2e' in sh.screen().splitlines(), 'the command did not run after the storm')


@scenario
def two_shells(env):
    """Two shells in different cwds each keep their own ranking."""
    a, b = env.shell(env.proj), env.shell(env.other)
    a.type('fake')
    b.type('fake')
    a.wait_ghost('app launch')
    b.wait_ghost('trace record')
    a.send('C-c')
    a.run('echo from-a-e2e')
    b.send('C-c')
    b.wait_buffer('')
    b.type('fake')
    b.wait_ghost('trace record')
    a.type('fake')
    a.wait_ghost('app launch')


@scenario
def incognito_history(env):
    """Empty fish_history (by hand or in config.fish) = nothing to the real history or the log."""
    sh = env.shell(env.work)
    sh.ranked()
    sh.run("set -g fish_history ''")
    sh.run('echo secret-one-e2e')
    sh.type('echo ')
    time.sleep(0.5)
    sh.type('secret-two-e2e')
    sh.run('')
    sh.run('set -e fish_history')
    sh.run('echo after-one-e2e')
    env.add_config("set -g fish_history ''")
    sh2 = env.shell(env.work)
    sh2.run('echo cfg-hidden-e2e')
    sh2.run('set -e fish_history')
    sh2.run('echo after-two-e2e')
    for c in ('echo after-one-e2e', 'echo after-two-e2e'):
        sh.wait(lambda: c in env.real_history() and c in env.log_cmds(), f'{c} did not reach the history and the log')
    leaked = [c for c in env.real_history() + env.log_cmds() if 'secret-' in c or 'hidden' in c]
    check(not leaked, f'saved with history off: {leaked}')


@scenario
def named_session_back(env):
    """set -e fish_history after a named session goes back to the normal session."""
    sh = env.shell(env.work)
    sh.ranked()
    sh.run('set -g fish_history zqproj')
    sh.run('echo in-proj-e2e')
    sh.run('set -e fish_history')
    sh.run('echo back-e2e')
    sh.wait(lambda: 'echo back-e2e' in env.real_history(), 'echo back-e2e did not go back to the normal history')
    proj = [i.cmd for i in common.read_fish_history(env.fish_data / 'zqproj_history')]
    check('echo in-proj-e2e' in proj and 'echo back-e2e' not in proj, f'zqproj_history: {proj}')


@scenario
def should_add_sees_typing_cwd(env):
    """fish_should_add_to_history judges with the cwd the line was typed in, not the one after."""
    (env.home / 'secret').mkdir()
    env.add_config('function fish_should_add_to_history\n'
                   '    string match -q "$HOME/secret*" -- $PWD; and return 1\n'
                   "    string match -q ' *' -- \"$argv\"; and return 1\n"
                   '    return 0\nend')
    sh = env.shell(env.home)
    sh.ranked()
    for c in ('cd ~/secret', 'echo classified-e2e', 'cd ~', 'echo after-e2e'):
        sh.run(c)
    sh.wait(lambda: 'echo after-e2e' in env.log_cmds(), 'echo after-e2e did not reach the log')
    inside = [r.cmd for r in env.log_recs() if r.cwd.startswith(str(env.home / 'secret'))]
    check(not inside, f'the log kept what was typed inside ~/secret: {inside}')


@scenario
def fallback_keeps_what_the_filter_keeps(env):
    """An execute from another plugin: what the user's fish_should_add_to_history accepts reaches the real history."""
    env.add_config('function fish_should_add_to_history\n'
                   "    string match -qr '^mysql' -- $argv; and return 1\n    return 0\nend\n"
                   'function zqrun_line; commandline -f execute; end\nbind ctrl-o zqrun_line')
    sh = env.shell(env.work)
    sh.ranked()
    n = sh.prompts()
    sh.type(' echo spaced-keep-e2e')
    sh.send('C-o')
    sh.wait_prompt(n + 1)
    sh.run('echo after-e2e')
    sh.wait(lambda: 'echo after-e2e' in env.real_history(), 'echo after-e2e did not reach the real history')
    check(' echo spaced-keep-e2e' in env.real_history(), 'fish keeps it without the plugin; with it, it got lost')
    check(not any('spaced-keep' in c for c in env.log_cmds()), 'the log kept a line with a leading space')


@scenario
def rebound_after_prompt(env):
    """A history-reading binding defined after the prompt is up (fisher update, fzf_configure_bindings) gets wrapped."""
    sh = env.shell(env.work)
    sh.ranked()
    sh.run('function zqlast; commandline -r -- $history[1]; end; bind ctrl-o zqlast')
    sh.run('echo last-run-e2e')
    sh.send('C-o')
    sh.wait_buffer('echo last-run-e2e')


@scenario
def fisher_remove(env):
    """fisher remove leaves the shell clean: no bindings, handlers or shadow history."""
    e = env.sibling(plugin=False)
    sh = e.shell(e.work)
    sh.run(f'fisher install {REPO} >/dev/null 2>&1', timeout=30)
    sh.wait(lambda: list(e.fish_data.glob('fishsense_*_history')), 'fisher install did not load the plugin')
    sh.run('echo before-remove-e2e')
    sh.run(f'fisher remove {REPO} >/dev/null 2>&1', timeout=30)
    sh.run('echo FS:' + ':'.join(f'({p} | count)' for p in PLUGIN_PROBES[:2]))
    sh.run('echo after-remove-e2e')
    screen = sh.screen()
    check('FS:0:0' in screen, f'plugin handlers or bindings were left:\n{screen}')
    check('Unknown command' not in screen, f'a binding calls something that no longer exists:\n{screen}')
    sh.wait(lambda: 'echo after-remove-e2e' in e.real_history(), 'the command did not reach the real history')
    check(not list(e.fish_data.glob('fishsense_*')), 'the shadow history was left behind')


@scenario
def leftovers(env):
    """exit, exec and half-written copies from dead shells leave no shadow histories or results."""
    stray = env.fish_data / '.fishsense_999999999_history.4242.tmp'
    stray.write_text('- cmd: echo leftover\n')
    log_copy = env.log.parent / '.log.tsv.999999999.tmp'
    log_copy.write_text('1\t1\t0\t%2F\techo%20leftover\n')
    sh = env.shell(env.work)
    check(not stray.exists(), 'startup did not remove the half-written copy of a dead shell')
    check(not log_copy.exists(), 'startup did not remove the half-written copy of the log')
    sh.ranked()
    prompts = env.run / 'e2e'
    n, before = sh.prompts(), set(prompts.iterdir())
    sh.type('fish')
    sh.send('Enter')
    inner = sh.wait(lambda: set(prompts.glob('*.prompt')) - before, 'the nested fish did not start')
    pid = int(inner.pop().name.split('.')[0])
    sh.wait(lambda: (env.fish_data / f'fishsense_{pid}_history').exists(), 'the nested fish did not make its shadow')
    sh.type('echo inner-e2e')
    sh.send('Enter')
    time.sleep(0.3)
    sh.type('exit')
    sh.send('Enter')
    sh.wait_prompt(n + 1)
    time.sleep(1)
    left = [p.name for p in (*env.fish_data.glob(f'fishsense_{pid}_*'), *(env.run / 'fishsense').glob(f'{pid}.*'))]
    check(not left, f'exit left: {left}')
    sh.type('exec sh')
    sh.send('Enter')
    time.sleep(1)
    left = [p.name for p in (*env.fish_data.glob(f'fishsense_{sh.pid}_*'), *(env.run / 'fishsense').glob(f'{sh.pid}.*'))]
    check(not left, f'exec left: {left}')


@scenario
def no_abbr_corrections(env):
    """A correction never lands on an abbreviation. A fix to a function is applied without asking."""
    env.add_config("abbr -a zqgp 'fakeapp pushed-e2e'\nfunction zqunbak; fakeapp unbaked-e2e $argv; end")
    sh = env.shell(env.work)
    sh.ranked()
    sh.run('zqgq')
    sh.run('zqgq x')
    check(not env.marks(), f'the abbreviation expansion ran: {env.marks()}')
    sh.wait(lambda: env.log_cmds()[-2:] == ['zqgq', 'zqgq x'], 'the original lines are not in the log')
    n = sh.prompts()
    sh.type('zqunbka a.bak')
    sh.send('Enter')
    sh.wait(lambda: any('unbaked-e2e' in m for m in env.marks()), 'the function was not corrected and run')
    check('did you mean' not in sh.screen(), f'it asked before correcting to a function: {sh.screen()}')
    sh.wait_prompt(n + 1)


@scenario
def subcommands_fresh(env):
    """Subcommands: a directory with ./install does not hide install elsewhere, nor does the cache hide a new one."""
    check(not shutil.which('fsnpm', path=env.env()['PATH']), 'fsnpm exists on this system; the scenario does not apply')
    (env.bin / 'fsnpm').write_text(FAKE_BIN)
    (env.bin / 'fsnpm').chmod(0o755)
    subs = env.home / 'fsnpm-subs'
    subs.write_text('install\nuninstall\nls\n')
    (env.fish_conf / 'completions' / 'fsnpm.fish').write_text(
        "complete -c fsnpm -f\ncomplete -c fsnpm -n __fish_use_subcommand -a '(cat ~/fsnpm-subs)'\n")
    a, b = env.home / 'proj-a', env.home / 'proj-b'
    (a / 'install').mkdir(parents=True)
    b.mkdir()
    sh = env.shell(a)
    sh.ranked()
    sh.run('fsnpm ls')
    sh.run(f'cd {b}')
    sh.run('fsnpm install')
    subs.write_text('install\nuninstall\nls\nlt\n')
    sh.run('fsnpm lt')
    sh.wait(lambda: env.marks()[-2:] == ['fsnpm install', 'fsnpm lt'], lambda: f'ran: {env.marks()}')
    check('corrected' not in sh.screen(), 'corrected a valid subcommand')


@scenario
def long_question(env):
    """The [Y/n] question for a long line shows the whole command, not its tail."""
    sh = env.shell(env.work)
    sh.ranked()
    n = sh.prompts()
    line = 'mr target.txt ' + ' '.join(f'no-such-file-{i:03}' for i in range(9))
    sh.type(line)
    sh.send('Enter')
    sh.wait(lambda: '[Y/n]' in sh.screen(), 'did not ask')
    check('did you mean: rm target.txt no-such-file-000' in sh.screen(), 'the start of the question is not visible')
    sh.send('n')
    if not sh.settle(lambda: sh.prompts() > n, 1):
        sh.send('Enter')
    sh.wait_prompt(n + 1)
    check((env.work / 'target.txt').exists(), 'target.txt was deleted')


@scenario
def forward_token_trim(env):
    """ctrl-→ (forward-token) also stops before the placeholder."""
    sh = env.shell(env.work)
    sh.type('tar ')
    sh.wait(lambda: '‹' in sh.ghost(), lambda: f'no ghost with ‹…›: {sh.ghost()!r}', 8)
    for _ in range(4):
        sh.send('C-Right')
        time.sleep(0.15)
    check('‹' not in sh.buffer(), f'the placeholder got into the buffer: {sh.buffer()!r}')


@scenario
def escaped_space_no_ghost(env):
    """An escaped space (half a file name) does not ask for the next parameter."""
    sh = env.shell(env.work)
    sh.type('waifu2x-ncnn-vulkan ')
    sh.wait(lambda: '-i' in sh.ghost(), lambda: f'the ghost does not offer -i: {sh.ghost()!r}', timeout=8)
    sh.send('C-c')
    sh.wait_buffer('')
    sh.type('waifu2x-ncnn-vulkan -i My\\ ')
    time.sleep(1.5)
    check('-o' not in sh.ghost(), f'ghost in the middle of a word: {sh.ghost()!r}')
    sh.type('Photos.png ')
    sh.wait(lambda: '-o' in sh.ghost(), lambda: f'no -o after the whole word: {sh.ghost()!r}', 8)


@scenario
def long_line_quiet(env):
    """A huge line does not make fish print exec errors when asking for parameters."""
    sh = env.shell(env.work)
    sh.ranked()
    n = sh.prompts()
    big = env.root / 'big.txt'
    big.write_text("echo '%s' | wc -c" % json.dumps({f'k{i:04}': 'v' * 40 for i in range(2000)}))
    tmux('load-buffer', '-b', 'e2e', str(big))
    tmux('paste-buffer', '-p', '-r', '-d', '-b', 'e2e', '-t', sh.name)
    for _ in range(2):
        sh.send('Space')
        time.sleep(0.5)
    sh.send('BSpace', 'BSpace')
    sh.send('Enter')
    sh.wait_prompt(n + 1)
    time.sleep(0.5)
    check('exec:' not in sh.screen() and 'argument' not in sh.screen(), 'fish printed an exec error')


@scenario
def git_alias_asks(env):
    """A correction that lands on a git alias (wipe = reset --hard) asks before running."""
    (env.home / '.gitconfig').write_text('[user]\n\tname = e2e\n\temail = e2e@example.invalid\n'
                                         '[alias]\n\twipe = reset --hard\n')
    notes = env.repo / 'notes.txt'
    notes.write_text('v1\n')
    for args in (['add', 'notes.txt'], ['commit', '-qm', 'v1']):
        subprocess.run(['git', '-C', str(env.repo), *args], env=env.env(), check=True, capture_output=True)
    notes.write_text('v1\nhours of unsaved work\n')
    sh = env.shell(env.repo)
    sh.ranked()
    n = sh.prompts()
    sh.type('git wpie')
    sh.send('Enter')
    sh.wait(lambda: 'did you mean: git wipe? [Y/n]' in sh.screen(), 'did not ask before a git alias')
    sh.send('n')
    if not sh.settle(lambda: sh.prompts() > n, 1):
        sh.send('Enter')
    sh.wait_prompt(n + 1)
    check('unsaved' in notes.read_text(), 'git wipe ran: the unsaved work is gone')


@scenario
def command_abbr_kept(env):
    """A subcommand abbreviation (abbr --command git lg) is not corrected: it expands."""
    env.add_config("abbr --add --command git lg 'log --oneline -1'")
    sh = env.shell(env.repo)
    sh.ranked()
    sh.run('git lg')
    check('corrected' not in sh.screen(), 'corrected the abbreviation as if it were a typo')
    sh.wait(lambda: env.real_history()[-1:] == ['git log --oneline -1'],
            lambda: f'real history ends with {env.real_history()[-2:]}')


@scenario
def named_session_keeps_log(env):
    """A shell with its own fish_history does not wipe the other shells' context log."""
    a = env.shell(env.proj)
    a.type('fake')
    a.wait_ghost('app launch')
    a.send('C-c')
    n = len(env.log_recs())
    b = env.shell(env.work)
    b.ranked()
    b.run('set -g fish_history zqscratch')
    b.run('echo in-scratch-e2e')
    b.run('echo again-e2e')
    b.wait(lambda: 'echo again-e2e' in env.log_cmds(), 'echo again-e2e did not reach the log')
    time.sleep(0.5)
    check(len(env.log_recs()) >= n, f'the log kept {len(env.log_recs())} of {n} records')
    a.run('cd .')
    a.type('fake')
    a.wait_ghost('app launch')


@scenario
def history_clear_clears_log(env):
    """history clear also empties the context log, the command that just ran included."""
    sh = env.shell(env.work)
    sh.ranked()
    sh.run('echo api-key-sk-e2e-999')
    sh.wait(lambda: 'echo api-key-sk-e2e-999' in env.log_cmds(), 'did not reach the log')
    n = sh.prompts()
    sh.type('history clear')
    sh.send('Enter')
    sh.wait(lambda: 'yes/no' in sh.screen(), 'history clear did not ask')
    sh.type('yes')
    sh.send('Enter')
    sh.wait_prompt(n + 1)
    check(env.real_history() == [], f'the real history is not empty: {env.real_history()}')
    sh.wait(lambda: not env.log_recs(), lambda: f'the log still has {len(env.log_recs())} records')


@scenario
def argv_private(env):
    """What the user types never goes on the helpers' command line (ps shows it to every account)."""
    seen, stop = [], threading.Event()

    def poll():
        while not stop.is_set():
            for p in Path('/proc').iterdir():
                try:
                    if p.name.isdigit() and b'fishsense.py' in (c := (p / 'cmdline').read_bytes()):
                        seen.append(c)
                except OSError:
                    pass
            time.sleep(0.002)
    t = threading.Thread(target=poll, daemon=True)
    t.start()
    try:
        sh = env.shell(env.work)
        sh.ranked()
        sh.type('mysql -u root -pHunter2-e2e ')
        time.sleep(1)
        sh.send('C-c')
        sh.run('echo Hunter2-e2e-ran')
        sh.wait(lambda: 'echo Hunter2-e2e-ran' in env.log_cmds(), 'did not reach the log')
    finally:
        stop.set()
        t.join()
    check(seen, 'no helper was seen running; the scenario proves nothing')
    leaked = [c for c in seen if b'Hunter2' in c or b'mysql' in c]
    check(not leaked, f'typed text shows up in ps: {leaked[:1]!r}')


@scenario
def shift_delete_validated(env):
    """shift-delete removes the suggestion fish shows, even with a newer one fish does not show."""
    with open(env.real_hist, 'a') as f:
        f.write(f'- cmd: zqfoo shown-e2e\n  when: {NOW - 300}\n- cmd: zqgone hidden-e2e\n  when: {NOW - 200}\n')
    sh = env.shell(env.work)
    sh.ranked()
    sh.type('zq')
    sh.wait_ghost('foo shown-e2e')
    sh.send('S-DC')
    sh.wait(lambda: 'zqfoo shown-e2e' not in env.real_history(), 'the suggestion is still in the real history')
    check('zqgone hidden-e2e' in env.real_history(), 'deleted another line from the real history')
    sh.send('C-c')
    sh.wait_buffer('')
    sh.type('zq')
    time.sleep(0.5)
    check(sh.ghost() != 'foo shown-e2e', 'the deleted suggestion came back')


@scenario
def pager_delete(env):
    """shift-delete in the history pager also removes the line from the suggestions."""
    env.add_config('bind ctrl-x history-pager')
    sh = env.shell(env.work)
    sh.ranked()
    sh.run('echo tok3n-secret-e2e')
    sh.run('echo other-e2e')
    sh.type('echo tok')
    sh.wait_ghost('3n-secret-e2e')
    sh.send('C-c')
    sh.wait_buffer('')
    sh.send('C-x')
    sh.type('tok3n')
    sh.wait_buffer('echo tok3n-secret-e2e')
    sh.send('S-DC')
    sh.wait(lambda: 'echo tok3n-secret-e2e' not in env.real_history(), 'it was not deleted from the real history')
    sh.send('Escape')
    sh.send('C-c')
    sh.wait(lambda: not sh.state()['paging'] and sh.buffer() == '', 'the pager did not close')
    sh.type('echo tok')
    time.sleep(0.5)
    check('secret' not in sh.ghost(), f'the deleted line is still suggested: {sh.ghost()!r}')


@scenario
def trim_multiline_and_undo(env):
    """The placeholder on line 2 does not touch line 1, and ctrl-z does not bring the ‹…› in."""
    sh = env.shell(env.work)
    sh.ranked()
    sh.type('echo a')
    sh.send('M-Enter')
    sh.type('tar ')
    sh.wait(lambda: '‹' in sh.ghost(), lambda: f'no ghost with ‹…›: {sh.ghost()!r}', 8)
    sh.send('Right')
    sh.wait_buffer('echo a\ntar -c -f ')
    sh.send('C-z')
    sh.wait_buffer('echo a\ntar ')


@scenario
def own_line_with_placeholder_char(env):
    """The user's own line with a ‹ is accepted whole: only a template is cut at the placeholder."""
    with open(env.real_hist, 'a') as f:
        f.write(f'- cmd: echo zqquote ‹hello› world\n  when: {NOW - 100}\n')
    sh = env.shell(env.work)
    sh.ranked()
    sh.type('echo zqq')
    sh.wait_ghost('uote ‹hello› world')
    sh.send('Right')
    sh.wait_buffer('echo zqquote ‹hello› world')
    time.sleep(0.3)
    check(sh.buffer() == 'echo zqquote ‹hello› world', f'it was cut at the ‹: {sh.buffer()!r}')


@scenario
def cancel_forgets_injected(env):
    """After ctrl-c, an AI or template ghost that was not accepted no longer beats the history."""
    fake = env.fake_ollama([('hello-e2e', 'echo hello-e2e world-ai')])
    sh = env.shell(env.work, OLLAMA_HOST=fake.host)
    sh.ranked()
    sh.type('echo hello-e2e')
    sh.send('C-Space')
    sh.wait_ghost(' world-ai', timeout=8)
    sh.send('C-c')
    sh.wait_buffer('')
    sh.type('echo h')
    time.sleep(0.5)
    check('world-ai' not in sh.ghost(), f'the rejected AI ghost is still there: {sh.ghost()!r}')
    sh.send('C-c')
    sh.run('true zqmine')
    sh.type('tar -x ')
    sh.wait(lambda: '‹' in sh.ghost(), lambda: f'no ghost with ‹…›: {sh.ghost()!r}', 8)
    sh.send('C-c')
    sh.wait_buffer('')
    sh.type('t')
    sh.wait_ghost('rue zqmine')


@scenario
def ghosts_append_cheaply(env):
    """One parameter ghost after another gets appended to the shadow history without rewriting it whole."""
    sh = env.shell(env.work)
    sh.ranked()
    shadow = env.fish_data / f'fishsense_{sh.pid}_history'
    sh.type('waifu2x-ncnn-vulkan ')
    sh.wait(lambda: '-i' in sh.ghost(), lambda: f'the ghost does not offer -i: {sh.ghost()!r}', 8)
    ino = shadow.stat().st_ino
    sh.type('-i a.png ')
    sh.wait(lambda: '-o' in sh.ghost(), lambda: f'the ghost does not offer -o: {sh.ghost()!r}', 8)
    check(shadow.stat().st_ino == ino, 'the shadow history was rewritten whole for one ghost')


@scenario
def ctrl_space_off(env):
    """With fishsense off, ctrl-space does what it always did (a space), not nothing."""
    sh = env.shell(env.work)
    sh.run('fishsense off')
    sh.type('echo a')
    sh.send('C-Space')
    sh.type('b')
    sh.wait_buffer('echo a b')


@scenario
def unwritable_files_quiet(env):
    """A log or cache it cannot write (full disk, root-owned files) prints no errors."""
    env.log.chmod(0o444)
    subs = env.cache / 'fishsense' / 'subcommands' / 'git'
    subs.parent.mkdir(parents=True, exist_ok=True)
    subs.write_text('status\n')
    subs.chmod(0o444)
    check(not os.access(env.log, os.W_OK), 'running as root the scenario does not apply')
    sh = env.shell(env.repo)
    sh.ranked()
    sh.run('echo one-e2e')
    sh.run('git stauts')
    sh.wait(lambda: env.real_history()[-1:] == ['git status'], 'did not fix git stauts')
    time.sleep(0.5)
    screen = sh.screen()
    check(not any(w in screen for w in ('warning:', 'Permission denied', 'No space left')),
          f'errors on screen:\n{screen}')


@scenario
def alt_s_prepends_last_command(env):
    """alt-s on an empty line prepends sudo to the last command run, not to the best-ranked one."""
    check(any(shutil.which(c, path=env.env()['PATH']) for c in ('sudo', 'doas', 'please', 'run0')),
          'no sudo/doas/please/run0; the scenario does not apply')
    sh = env.shell(env.repo)
    sh.ranked()
    sh.run('git log --oneline >/dev/null 2>&1')
    time.sleep(1)
    sh.send('M-s')
    buf = sh.wait(lambda: sh.buffer(), 'alt-s did nothing')
    check(buf.endswith(' git log --oneline >/dev/null 2>&1'), f'alt-s gave {buf!r}')


@scenario
def last_pid_is_the_users(env):
    """$last_pid stays the one of the user's `cmd &`, even when helpers run after it."""
    sh = env.shell(env.work)
    sh.ranked()
    sh.run('sleep 300 &')
    sh.type('echo ')
    time.sleep(0.5)
    sh.send('C-c')
    sh.run('echo LAST=$last_pid JOB=(jobs -p)')
    m = re.search(r'LAST=(\d+) JOB=(\d+)', sh.screen())
    check(m and m.group(1) == m.group(2), f'$last_pid is not the background job: {m and m.group(0)}')
    sh.run('kill $last_pid; sleep 0.3; echo JOBS_LEFT=(jobs -p | count)')
    check('JOBS_LEFT=0' in sh.screen(), 'kill $last_pid did not kill the background job')


@scenario
def fish_c_off_on(env):
    """fishsense off/on from fish -c (a script, a binding) works and exits 0."""
    for cmd, want in (('fishsense off', 'off'), ('fishsense on', 'on')):
        r = subprocess.run(['fish', '-c', cmd], env=env.env(), capture_output=True, text=True, timeout=30)
        check(r.returncode == 0, f'{cmd}: exits {r.returncode}: {r.stderr.strip()}')
        r = subprocess.run(['fish', '-c', 'set -q fishsense_disabled; and echo off; or echo on'], env=env.env(),
                           capture_output=True, text=True, timeout=30)
        check(r.stdout.strip() == want, f'after {cmd}: {r.stdout.strip()}')


@scenario
def tool_added_to_path_later(env):
    """A command that joins PATH later (nvm use) still gets its generated completions."""
    src = shutil.which('waifu2x-ncnn-vulkan', path=env.env()['PATH'])
    check(src, 'waifu2x-ncnn-vulkan is missing; the scenario does not apply')
    nvm = env.home / 'nvmbin'
    nvm.mkdir()
    shutil.copy(src, nvm / 'zqtool')
    sh = env.shell(env.work)
    sh.ranked()
    sh.type('zqtool ')
    time.sleep(0.5)
    sh.send('C-c')
    sh.run(f'set -gx PATH {nvm} $PATH')
    sh.type('zqtool ')
    comp = env.cache / 'fishsense' / 'completions' / 'zqtool.fish'
    sh.wait(comp.exists, f'{comp} was not generated', timeout=8)


PLUGIN_PROBES = ('functions --handlers | string match -e __fishsense',
                 "bind -a | string match -v '#*' | string match -e __fishsense",
                 "set -n | string match -r '^_*fishsense'")


@scenario
def private_mode(env):
    """fish --private neither loads the plugin nor writes anything."""
    before = env.plugin_files()
    sh = env.shell(env.proj, args=('--private',))
    sh.run('echo FS:' + ':'.join(f'({p} | count)' for p in PLUGIN_PROBES))
    check('FS:0:0:0' in sh.screen(), 'the plugin is active in private mode (handlers:bindings:variables ≠ 0)')
    sh.run('echo private-e2e')
    time.sleep(0.5)
    check(env.plugin_files() == before, 'the plugin wrote files in private mode')
    check(not any('private-e2e' in c for c in env.real_history()), 'the real history changed in private mode')


def _median_run(env, argv, n=10):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        subprocess.run(argv, env=env.env(), cwd=env.work, capture_output=True, timeout=30)
        ts.append(time.perf_counter() - t0)
    return statistics.median(ts)


@scenario
def fish_c_untouched(env):
    """fish -c touches nothing of the plugin and stays just as fast."""
    before = env.plugin_files()
    out = subprocess.run(['fish', '-c', '; '.join(PLUGIN_PROBES) + '; true'], env=env.env(), cwd=env.work,
                         capture_output=True, text=True, timeout=30)
    check(out.stdout.strip() == '' and out.stderr.strip() == '', f'fish -c sees the plugin: {out.stdout}{out.stderr}')
    check(env.plugin_files() == before, 'fish -c wrote plugin files')
    base = env.sibling(plugin=False)
    d = _median_run(env, ['fish', '-c', 'true']) - _median_run(base, ['fish', '-c', 'true'])
    check(d <= 0.015, f'fish -c takes {d * 1000:+.1f} ms vs the baseline (max +15)')


@scenario
def startup_budget(env):
    """The first interactive prompt takes ≤ 80 ms more than without the plugin."""
    base = env.sibling(plugin=False)
    ts = {id(env): [], id(base): []}
    for i in range(8):
        for e in (base, env):
            t0 = time.perf_counter()
            sh = e.shell(e.work)
            if i:
                ts[id(e)].append(time.perf_counter() - t0)
            sh.kill()
            time.sleep(0.15)
    p, b = statistics.median(ts[id(env)]), statistics.median(ts[id(base)])
    check(p - b <= 0.080, f'first prompt {p * 1000:.0f} ms with the plugin vs {b * 1000:.0f} ms without')


@scenario
def off_on(env):
    """fishsense off goes back to native behaviour; on brings it back."""
    sh = env.shell(env.proj)
    sh.type('fake')
    sh.wait_ghost('app launch')
    sh.send('C-c')
    sh.run('fishsense off')
    os.kill(sh.pid, signal.SIGUSR1)
    time.sleep(0.2)
    check(sh.alive(), 'fish died from a late SIGUSR1 after fishsense off')
    sh.type('fake')
    sh.wait_ghost('trace record')
    sh.send('C-c')
    sh.run('echo off-e2e')
    sh.wait(lambda: 'echo off-e2e' in env.real_history(), 'with it off, the command did not reach the real history')
    sh.run('fishsense on')
    sh.type('fake')
    sh.wait_ghost('app launch')


def main():
    ap = argparse.ArgumentParser(description='fishsense end-to-end tests in tmux.')
    ap.add_argument('-k', default='', metavar='TEXT', help='only scenarios whose name contains TEXT')
    ap.add_argument('--keep', action='store_true', help='keep tmux and the directories for inspection')
    ap.add_argument('--list', action='store_true', help='list the scenarios and exit')
    a = ap.parse_args()
    picked = [f for f in SCENARIOS if a.k in f.__name__]
    if a.list:
        for f in picked:
            print(f'{f.__name__:24} {f.__doc__}')
        return 0
    if not picked:
        print(f'no scenario contains {a.k!r}', file=sys.stderr)
        return 2
    missing = [t for t in ('tmux', 'fish', 'git') if not shutil.which(t)]
    if missing:
        print(f'missing: {", ".join(missing)}', file=sys.stderr)
        return 2
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda s, _: sys.exit(128 + s))
    results = []
    try:
        for fn in picked:
            print(f'{fn.__name__} …', end=' ', flush=True)
            t0, env, err = time.monotonic(), None, ''
            try:
                env = Env()
                fn(env)
            except Exception as e:
                err = f'{e}' if isinstance(e, Fail) else traceback.format_exc()
                if env:
                    err += '\n' + env.screens()
            finally:
                if env:
                    env.close(a.keep)
            results.append((fn.__name__, err, time.monotonic() - t0))
            print('ok' if not err else 'FAIL', flush=True)
    finally:
        if not a.keep:
            tmux('kill-server', check=False)
            Path(os.environ.get('TMUX_TMPDIR', '/tmp'), f'tmux-{os.getuid()}', SOCK).unlink(missing_ok=True)
    print()
    for name, err, dt in results:
        print(f'{"FAIL" if err else "PASS"}  {name:24} {dt:5.1f} s  {err.splitlines()[0] if err else ""}')
    failed = [(n, e) for n, e, _ in results if e]
    for name, err in failed:
        print(f'\n=== {name}\n{err}')
    print(f'\n{len(results) - len(failed)}/{len(results)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
