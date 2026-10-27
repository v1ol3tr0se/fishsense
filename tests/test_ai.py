import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HELPER_DIR = Path(__file__).resolve().parent.parent / 'conf.d' / 'fishsense'
sys.path.insert(0, str(HELPER_DIR))
import ai
import common


class Fake(BaseHTTPRequestHandler):
    """Stand-in for ollama: canned reply (a list: one per request, the last one repeats), records every
    request body."""
    reply, status, delay, models, vram = '', 200, 0, [], 0
    requests, got_request = [], threading.Event()

    def do_POST(self):
        Fake.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        Fake.got_request.set()
        if Fake.status != 200:
            return self._send(Fake.status, {'error': f"model '{Fake.requests[-1]['model']}' not found"})
        r = Fake.reply
        if isinstance(r, list):
            r = r[min(len(Fake.requests), len(r)) - 1]
        cut = r.endswith('…cut')
        self._send(200, {'model': 'x', 'message': {'role': 'assistant', 'content': r.removesuffix('…cut')},
                         'done': True, 'done_reason': 'length' if cut else 'stop'})

    def do_GET(self):
        self._send(200, {'models': [{'name': n, 'size_vram': Fake.vram} for n in Fake.models]})

    def _send(self, status, obj):
        time.sleep(Fake.delay)
        try:
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    def log_message(self, *a):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class AiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {f'XDG_{k}_HOME': str(self.tmp / k.lower()) for k in ('DATA', 'CACHE', 'CONFIG')}
        env.update(XDG_RUNTIME_DIR=str(self.tmp / 'run'), HOME=str(self.tmp / 'home'),
                   OLLAMA_HOST=f'127.0.0.1:{self.srv.server_port}',
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop('FISHSENSE_AI_MODEL', None)
        self.power(online=1)
        Fake.reply, Fake.status, Fake.delay, Fake.models, Fake.vram = '', 200, 0, [], 0
        Fake.requests.clear()
        Fake.got_request.clear()
        self.cwd = self.tmp / 'proj'
        (self.cwd / 'src').mkdir(parents=True)
        (self.cwd / 'Makefile').touch()
        (self.cwd / 'my notes.txt').touch()
        self.fish_data = self.tmp / 'data' / 'fish'
        self.fish_data.mkdir(parents=True)

    def power(self, online):
        d = self.tmp / 'power' / 'AC0'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'type').write_text('Mains\n')
        (d / 'online').write_text(f'{online}\n')
        (self.tmp / 'power' / 'BAT0').mkdir(exist_ok=True)
        (self.tmp / 'power' / 'BAT0' / 'type').write_text('Battery\n')
        p = mock.patch.object(ai, 'POWER_DIR', self.tmp / 'power')
        p.start()
        self.addCleanup(p.stop)

    def args(self, buffer):
        return ['--cwd', common.enc(str(self.cwd)), '--buffer', common.enc(buffer),
                '--fish-data', str(self.fish_data), '--real-name', 'fish']

    def run_ai(self, buffer, reply='', comps='', extra=()):
        """(kind, line), or None when it prints nothing. comps: what fish pipes in (complete -C)."""
        Fake.reply = reply
        out = io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(comps.encode()))
        with redirect_stdout(out), mock.patch.object(sys, 'stdin', stdin):
            self.assertEqual(ai.main('ai', self.args(buffer) + list(extra)), 0)
        if not out.getvalue():
            return None
        kind, payload = out.getvalue().rstrip('\n').split('\t')
        return kind, common.dec(payload)

    def prompt(self, i=-1):
        """The question about the line: the last user turn, after the system prompt and the examples."""
        msgs = Fake.requests[i]['messages']
        self.assertEqual([m['role'] for m in msgs[:3]], ['system', 'user', 'assistant'])
        return [m for m in msgs if m['role'] == 'user'][-1]['content']

    def test_ghost_when_reply_extends_buffer(self):
        self.assertEqual(self.run_ai('git com', 'git commit -m "fix"'), ('ghost', 'git commit -m "fix"'))
        self.assertEqual(self.run_ai(' git com', 'git commit'), ('ghost', ' git commit'))

    def test_replace_otherwise(self):
        self.assertEqual(self.run_ai('find todo', "grep -rn TODO ."), ('replace', 'grep -rn TODO .'))
        self.assertEqual(self.run_ai('', 'make'), ('replace', 'make'))

    def test_cleanup(self):
        cases = {
            '```fish\ngit status\n```': 'git status',
            '```fish\ngit status': 'git status',
            'Here you go:\n```\nls -la\n```': 'ls -la',
            '```ls -la```': 'ls -la',
            '$ ls -la  ': 'ls -la',
            '❯ ls -la': 'ls -la',
            '`ls -la`': 'ls -la',
            '"ls -la"': 'ls -la',
            'answer: ls -la': 'ls -la',
            '\nls -la\nexplanation here': 'ls -la',
            'echo "a" "b"': 'echo "a" "b"',
        }
        for reply, want in cases.items():
            with self.subTest(reply=reply):
                self.assertEqual(self.run_ai('list it all', reply), ('replace', want))

    def test_control_chars_stripped(self):
        kind, line = self.run_ai('', 'ls \x1b[31m-la\x07\x9b\rrm -rf /')
        self.assertEqual(kind, 'replace')
        self.assertFalse(any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f for c in line), repr(line))
        self.assertNotIn('rm -rf', line)

    def test_nothing_new_is_quiet(self):
        self.assertIsNone(self.run_ai('', '```\n```'))
        self.assertEqual(len(Fake.requests), 1)
        Fake.requests.clear()
        self.assertIsNone(self.run_ai('git status', 'git status'))
        self.assertEqual(self.prefills(), [None, 'git status'])

    def prefills(self):
        return [r['messages'][-1]['content'] if r['messages'][-1]['role'] == 'assistant' else None
                for r in Fake.requests]

    def test_retry_continues_the_line(self):
        self.assertEqual(self.run_ai('git sta', ['git sta', 'tus -s']), ('ghost', 'git status -s'))
        self.assertEqual(self.prefills(), [None, 'git sta'])
        self.assertEqual(self.prompt(0), self.prompt(1))
        Fake.requests.clear()
        self.assertEqual(self.run_ai('git sta', ['git', 'sh pop']), ('ghost', 'git stash pop'))
        Fake.requests.clear()
        self.assertEqual(self.run_ai('exiftool -all', ['exiftool', '= a.jpg']), ('ghost', 'exiftool -all= a.jpg'))
        self.assertEqual(self.prefills(), [None, 'exiftool -all'])
        Fake.requests.clear()
        self.assertEqual(self.run_ai('show disk usage', 'du -sh'), ('replace', 'du -sh'))
        self.assertEqual(len(Fake.requests), 1)

    def test_unfinished_answer_is_nothing(self):
        loop = 'exiftool -all= ' + '-tagsfromfile a.jpg -all= ' * 12
        self.assertIsNone(self.run_ai('exiftool -all= x.jpg', loop + '…cut'))
        self.assertEqual(self.run_ai('exiftool -all= x.jpg', 'exiftool -all= x.jpg -overwrite_original'),
                         ('ghost', 'exiftool -all= x.jpg -overwrite_original'))

    def test_no_retry_for_plain_words(self):
        self.assertIsNone(self.run_ai('comprime la carpeta photos', 'comprime la carpeta photos'))
        self.assertEqual(self.prefills(), [None])

    def test_multiline_said_back_is_nothing(self):
        buf = 'for f in *.png\n    echo $f\nend'
        self.assertIsNone(ai.decide(buf, 'for f in *.png; echo $f; end'))
        self.assertIsNone(ai.decide(buf, 'for f in *.png;     echo $f; end'))
        self.assertEqual(ai.decide(buf, 'for f in *.png; echo $f; magick $f x.webp; end')[0], 'replace')

    def test_one_budget_for_both_tries(self):
        Fake.delay = 1
        with mock.patch.object(ai, 'TIMEOUT', 2.5):
            self.assertIsNone(self.run_ai('git status', 'git status'))
        self.assertEqual(len(Fake.requests), 1)

    def test_command_word_settled_first(self):
        (self.fish_data / 'fish_history').write_text(common.format_fish_history(
            [('waifu2x -s 2 -i a.png', ()), ('wait', ()), ('waifu2x -i b.png', ())], when=10))
        self.assertEqual(self.run_ai(' wai', ' -i c.png', comps='wait\n'), ('ghost', ' waifu2x -i c.png'))
        self.assertEqual(self.prefills(), ['waifu2x'])
        self.assertTrue(self.prompt().endswith('\nline: waifu2x '), self.prompt())
        self.assertEqual(ai.habits('waifu2x ', ['waifu2x -s 2', 'wait', 'waifu2x-ncnn-vulkan x']), ['waifu2x -s 2'])
        self.assertNotIn('completions:', self.prompt())
        Fake.requests.clear()
        self.assertEqual(self.run_ai('wait', ' %1', comps='wait\nwaitpid\n'), ('ghost', 'wait %1'))
        self.assertEqual(self.prefills(), ['wait'])
        Fake.requests.clear()
        self.assertIsNone(self.run_ai('wait', '', comps='wait\n'))
        Fake.requests.clear()
        self.assertEqual(self.run_ai('waif', ''), ('ghost', 'waifu2x'))
        Fake.requests.clear()
        self.assertEqual(self.run_ai('zqhola', 'echo hola'), ('replace', 'echo hola'))
        self.assertEqual(self.prefills(), [None])

    def test_command_word(self):
        hist = ['waifu2x a', 'waifu2x b', 'waifu2x-ncnn-vulkan c', 'git', ' ', '']
        self.assertEqual(ai.command_word('wai', ['wait', 'waifu2x'], hist), 'waifu2x')
        self.assertEqual(ai.command_word('wait', ['wait'], hist), 'wait')
        self.assertEqual(ai.command_word('mak', ['make'], hist), 'make')
        self.assertIsNone(ai.command_word('zq', ['make'], hist))

    def test_request_shape(self):
        os.environ['FISHSENSE_AI_MODEL'] = 'llama3.2:3b'
        self.run_ai('ls -l', 'ls -la')
        req = Fake.requests[-1]
        self.assertEqual(req['model'], 'llama3.2:3b')
        self.assertEqual((req['stream'], req['think'], req['keep_alive']), (False, False, ai.KEEP_ALIVE))
        self.assertEqual(req['options']['temperature'], 0.2)
        self.assertLessEqual(req['options']['num_predict'], 100)
        self.assertNotIn('num_gpu', req['options'])
        self.assertEqual(req['messages'][0], {'role': 'system', 'content': ai.SYSTEM})
        self.assertTrue(self.prompt().endswith('\nline: ls -l'))
        self.assertEqual(len(req['messages']), 2 + 2 * len(ai.SHOTS))
        self.assertEqual(req['messages'][-1]['role'], 'user')
        self.assertTrue(all(len(x) == 2 and x[1] and '\n' not in x[1] for x in ai.SHOTS), ai.SHOTS)

    def test_warm(self):
        (self.fish_data / 'fish_history').write_text(common.format_fish_history([('make test', ())], when=10))
        out = io.StringIO()
        with redirect_stdout(out):
            ai.main('ai', ['--warm', '1'] + self.args('')[:2] + self.args('')[4:])
        self.assertEqual(out.getvalue(), '')
        warm = Fake.requests[-1]
        self.assertEqual(warm['options']['num_predict'], 1)
        self.run_ai('git sta', 'git stash', comps='stash\nstatus\n')
        real = Fake.requests[-1]
        self.assertEqual(warm['messages'][:-1], real['messages'][:-1])
        self.assertTrue(self.prompt().startswith(warm['messages'][-1]['content'] + '\n'), (warm, real))
        Fake.requests.clear()
        self.power(online=0)
        Fake.models = [ai.model()]
        ai.main('ai', ['--warm', '1'] + self.args('')[:2])
        self.assertEqual(Fake.requests, [])
        Fake.vram = 10 ** 9
        ai.main('ai', ['--warm', '1'] + self.args('')[:2])
        self.assertEqual(Fake.requests, [{'model': ai.model(), 'keep_alive': 0}])

    def test_battery_keeps_model_on_cpu(self):
        self.power(online=0)
        self.run_ai('ls', 'ls -la')
        self.assertEqual(Fake.requests[-1]['options']['num_gpu'], 0)

    def test_context(self):
        common.write_log([common.LogRec(1, 1, 0, str(self.cwd), 'make test'),
                          common.LogRec(2, 1, 0, '/elsewhere', 'htop'),
                          common.LogRec(3, 1, 0, str(self.cwd), 'deleted secret')])
        (self.fish_data / 'fish_history').write_text(common.format_fish_history(
            [('make test', ()), ('htop', ()), ('vim notes\nwq', ())], when=10))
        self.run_ai('', 'make')
        p = self.prompt()
        self.assertIn(f'cwd: {self.cwd}', p)
        self.assertIn("files: Makefile 'my notes.txt' src/", p)
        self.assertNotIn('deleted secret', p)
        self.assertIn('vim notes; wq', p)
        self.assertLess(p.index('htop'), p.index('make test'))
        self.assertTrue(p.endswith('\nline: (empty)'))
        self.assertNotIn('git:', p)
        self.assertNotIn('history:', p)
        self.assertNotIn('completions:', p)

    def test_habits(self):
        hist = ['git push', 'git status', 'waifu2x -s 2 -i a.png', 'git status', 'git stash pop',
                'waifu2x-ncnn-vulkan -i b.png', 'wait', 'git stash', 'git']
        self.assertEqual(ai.habits('git sta', hist), ['git status', 'git stash', 'git stash pop', 'git push'])
        self.assertEqual(ai.habits('wai', hist), ['wait', 'waifu2x-ncnn-vulkan -i b.png', 'waifu2x -s 2 -i a.png'])
        self.assertEqual(ai.habits('wait ', hist), [])
        self.assertEqual(ai.habits('', hist), [])
        self.assertEqual(len(ai.habits('git', hist * 5, n=3)), 3)

    def test_habits_in_prompt(self):
        (self.fish_data / 'fish_history').write_text(common.format_fish_history(
            [('make -j8 install', ())] + [(f'echo {i}', ()) for i in range(10)] + [('make test', ())], when=10))
        self.run_ai('make', 'make -j8')
        p = self.prompt()
        self.assertIn('history:\n  make -j8 install\n', p)
        self.assertEqual(ai.habits('make', ['make test', 'make -j8 install']), ['make -j8 install', 'make test'])

    def test_async_space_only_ghosts(self):
        with mock.patch.object(ai.common, 'notify') as notify:
            out = io.StringIO()
            with redirect_stdout(out):
                ai.main('ai', self.args('nmap ') + ['--notify', '1', '--pid', '4242', '--id', '7'])
            self.assertEqual(out.getvalue(), '')
            notify.assert_not_called()
            Fake.reply = 'nmap -sV scanme'
            out = io.StringIO()
            with redirect_stdout(out):
                ai.main('ai', self.args('nmap ') + ['--notify', '1', '--pid', '4242', '--id', '7'])
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[:2] + (notify.call_args.args[3],), ('4242', 'ghost', 'nmap -sV scanme'))
            self.assertEqual(out.getvalue(), '')
            notify.reset_mock()
            ai.main('ai', self.args('find big files') + ['--notify', '1', '--pid', '4242', '--id', '8'])
            notify.assert_not_called()
            notify.reset_mock()
            os.environ['OLLAMA_HOST'] = f'127.0.0.1:{free_port()}'
            ai.main('ai', self.args('nmap ') + ['--notify', '1', '--pid', '4242', '--id', '9'])
            notify.assert_not_called()

    def test_superseded_skips_inference(self):
        (ai.common.run_dir() / '4242.aireq').write_text('9\n')
        with mock.patch.object(ai.common, 'notify') as notify:
            ai.main('ai', self.args('nmap ') + ['--notify', '1', '--pid', '4242', '--id', '7'])
        self.assertEqual(Fake.requests, [])
        notify.assert_not_called()

    def test_custom_command_hint(self):
        self.assertEqual(ai.cmd_hint('sethost ', 'sets the host', True), 'command: sethost — sets the host')
        self.assertEqual(ai.cmd_hint('fakeview ', '', True),
                         'command: fakeview (a custom command; use it as-is unless you know its flags)')
        self.assertEqual(ai.cmd_hint('nmap ', '', False), '')
        self.assertEqual(ai.cmd_hint('', '', True), '')
        self.run_ai('fakeview', 'fakeview', comps='fakeview\n')
        self.assertNotIn('command:', self.prompt())
        Fake.requests.clear()
        self.run_ai('fakeview', 'fakeview --window', comps='fakeview\n',
                    extra=['--cmd-custom', '1', '--cmd-desc', common.enc('shows the window')])
        self.assertIn('command: fakeview — shows the window', self.prompt())

    def test_target_in_prompt(self):
        self.assertNotIn('target:', (self.run_ai('nmap', 'nmap -sV x'), self.prompt())[1])
        Fake.requests.clear()
        conf = self.tmp / 'config'
        conf.mkdir(parents=True, exist_ok=True)
        (conf / 'target').write_text('203.0.113.10\n')
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(conf)}), \
             mock.patch.object(ai.subprocess, 'run', return_value=mock.Mock(stdout='tun0  UNKNOWN  198.51.100.7/24\n')):
            self.run_ai('nmap', 'nmap -sV 203.0.113.10')
        self.assertIn('target: 203.0.113.10  vpn: 198.51.100.7', self.prompt())

    def test_usual_on_empty_line(self):
        (self.fish_data / 'fish_history').write_text(common.format_fish_history(
            [('git push', ()), ('cargo build', ()), ('htop', ())] + [(f'echo {i}', ()) for i in range(8)], when=10))
        (self.fish_data / 'fishsense_4242_history').write_text(common.format_fish_history(
            [('tar cf ‹target.tar› ‹files›', ()), ('htop', ()), ('git push', ()), ('cargo build', ()),
             ('git stash pop --ai-ghost', ())], when=10))
        hist = ['git push', 'cargo build', 'htop']
        self.assertEqual(ai.usual(str(self.fish_data), '4242', hist), ['cargo build', 'git push', 'htop'])
        self.assertEqual(ai.usual(str(self.fish_data), '4242', hist, n=1), ['cargo build'])
        self.assertEqual(ai.usual(str(self.fish_data), '999999', hist), [])
        self.run_ai('', 'cargo run')
        self.assertNotIn('history:', self.prompt())
        Fake.reply = 'cargo run'
        with redirect_stdout(io.StringIO()), mock.patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO())):
            ai.main('ai', self.args('') + ['--pid', '4242'])
        self.assertIn('history:\n  cargo build\n  git push\n  htop\nline: (empty)', self.prompt())

    def test_names(self):
        bin_ = self.tmp / 'bin'
        bin_.mkdir()
        for n in ('waifu2x-ncnn-vulkan', 'waitpid', 'zz'):
            (bin_ / n).touch(mode=0o755)
        with mock.patch.dict(os.environ, PATH=str(bin_)):
            self.assertEqual(ai.names('waifu2x\nwait\n\x1b[31mwaix\n', 'wai'),
                             ['waifu2x', 'wait', '[31mwaix', 'waifu2x-ncnn-vulkan', 'waitpid'])
        self.run_ai('git checkout fe', 'git checkout feature/x', comps='feature/x\n')
        self.assertNotIn('feature/x', self.prompt())

    @unittest.skipUnless(shutil.which('git'), 'git missing')
    def test_git_status_in_repo(self):
        subprocess.run(['git', 'init', '-q', str(self.cwd)], check=True)
        self.run_ai('', 'make')
        p = self.prompt()
        self.assertIn('git:\n  ## ', p)
        self.assertIn('?? Makefile', p)

    def test_errors(self):
        Fake.status = 404
        kind, msg = self.run_ai('ls', 'ls -la')
        self.assertEqual(kind, 'error')
        self.assertIn(ai.DEFAULT_MODEL, msg)
        self.assertIn('fishsense setup-ai', msg)

        Fake.status, Fake.delay = 200, 1
        with mock.patch.object(ai, 'TIMEOUT', 0.2):
            self.assertIsNone(self.run_ai('ls', 'ls -la'))

        os.environ['OLLAMA_HOST'] = f'127.0.0.1:{free_port()}'
        self.assertEqual(self.run_ai('ls', 'ls -la'), ('error', 'ollama is not responding; run: fishsense setup-ai'))

    def test_base_url(self):
        cases = {'': 'http://127.0.0.1:11434', '0.0.0.0': 'http://0.0.0.0:11434',
                 ':9999': 'http://127.0.0.1:9999', 'box:1234': 'http://box:1234',
                 'http://box': 'http://box:80', 'https://box/ollama/': 'https://box:443/ollama',
                 '[::1]:11434': 'http://[::1]:11434'}
        for raw, want in cases.items():
            with self.subTest(raw=raw), mock.patch.dict(os.environ, OLLAMA_HOST=raw):
                self.assertEqual(ai.base_url(), want)

    def test_doctor(self):
        Fake.models = [ai.DEFAULT_MODEL]
        lines = ai.doctor_lines()
        self.assertIn('responding at', lines[0])
        self.assertIn('installed', lines[1])
        self.assertIn('plugged in', lines[2])
        Fake.models = ['llama3.2:latest']
        os.environ['FISHSENSE_AI_MODEL'] = 'llama3.2'
        self.assertIn('installed', ai.doctor_lines()[1])
        os.environ['FISHSENSE_AI_MODEL'] = 'phi4'
        self.assertIn('fishsense setup-ai', ai.doctor_lines()[1])
        self.power(online=0)
        os.environ['OLLAMA_HOST'] = f'127.0.0.1:{free_port()}'
        lines = ai.doctor_lines()
        self.assertIn('not responding', lines[0])
        self.assertIn('battery', lines[-1])

    def test_cli_and_ctrl_c(self):
        cmd = [sys.executable, '-B', str(HELPER_DIR / 'fishsense.py'), 'ai'] + self.args('git com')
        Fake.reply = 'git commit'
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, input='')
        self.assertEqual((r.stdout, r.stderr), (f'ghost\t{common.enc("git commit")}\n', ''))

        Fake.got_request.clear()
        Fake.delay = 3
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertTrue(Fake.got_request.wait(5))
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=5)
        self.assertEqual((out, err, proc.returncode), ('', '', 130))


if __name__ == '__main__':
    unittest.main()
