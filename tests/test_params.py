import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'conf.d' / 'fishsense'))
import common
import params

FIX = Path(__file__).resolve().parent / 'fixtures' / 'params'
FISH = shutil.which('fish')


def help_of(name):
    return params.parse_help((FIX / 'help' / name).read_text(), man=name.endswith('.man.txt'))


def opt(info, name):
    for o in info['opts']:
        if name in params._names(o):
            return o
    raise AssertionError(f'{name} not parsed')


class Isolated(unittest.TestCase):
    """Every test gets its own XDG dirs; nothing touches the real cache or fish data."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='fishsense-test-'))
        env = {k: str(self.tmp / k.lower()) for k in
               ('XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_RUNTIME_DIR')}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.no_man = mock.patch.object(params, '_man', return_value='')
        self.no_man.start()

    def tearDown(self):
        self.no_man.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install_tldr(self):
        shutil.copytree(FIX / 'tldr', params.tldr_dir())
        params.build_index(params.tldr_dir())

    def seed_help(self, name, fixture):
        """Cached help for a name that is not on PATH (no key to validate)."""
        common.write_atomic(common.cache_dir() / 'help' / f'{name}.json',
                            __import__('json').dumps(help_of(fixture)))


class HelpStyles(unittest.TestCase):
    def test_waifu2x_short_flags_values_and_required(self):
        info = help_of('waifu2x-ncnn-vulkan.txt')
        self.assertEqual(info['usage'], [[('flag', '-i', 'infile'), ('flag', '-o', 'outfile')]])
        i, f, s, t = (opt(info, n) for n in ('-i', '-f', '-s', '-t'))
        self.assertEqual((i['arg'], i['file'], i['req']), ('input-path', True, True))
        self.assertEqual(f['vals'], ['jpg', 'png', 'webp'])
        self.assertEqual(s['vals'], ['1', '2', '4', '8', '16', '32'])
        self.assertEqual(opt(info, '-n')['vals'], ['-1', '0', '1', '2', '3'])
        self.assertEqual(t['vals'], [])
        self.assertFalse(f['req'])
        self.assertIsNone(opt(info, '-x')['arg'])

    def test_gnu_desc_on_next_line(self):
        info = help_of('ls.txt')
        self.assertGreater(len(info['opts']), 40)
        a, color, bs = opt(info, '--all'), opt(info, '--color'), opt(info, '--block-size')
        self.assertEqual((a['s'], a['arg']), (['a'], None))
        self.assertIn('do not ignore', a['d'])
        self.assertEqual((color['arg'], color['opt']), ('WHEN', True))
        self.assertEqual(bs['arg'], 'SIZE')
        self.assertIn('--author', params._names(opt(info, '--author')))

    def test_gnu_usage_variants(self):
        info = help_of('cp.txt')
        self.assertEqual(info['usage'][0], [('arg', 'SOURCE'), ('arg', 'DEST')])
        self.assertEqual(len(info['usage']), 3)
        self.assertFalse(opt(info, '-t')['req'])
        self.assertTrue(opt(info, '-t')['file'])
        self.assertEqual(opt(info, '--update')['vals'], ['all', 'none', 'none-fail', 'older'])

    def test_clap_and_cobra(self):
        uv = help_of('uv.txt')
        self.assertEqual(opt(uv, '--color')['vals'], ['auto', 'always', 'never'])
        self.assertTrue(opt(uv, '--cache-dir')['file'])
        self.assertEqual(opt(uv, '--cache-dir')['arg'], 'CACHE_DIR')
        self.assertIn(('run', 'Run a command or script'), uv['subs'])
        self.assertEqual(uv['usage'], [[('arg', 'COMMAND')]])
        for f, sub in (('dms.txt', 'screenshot'), ('ollama.txt', 'pull'), ('starship.txt', 'prompt'),
                       ('go.txt', 'build'), ('lazygit.txt', 'completion')):
            self.assertIn(sub, [s for s, _ in help_of(f)['subs']], f)
        self.assertNotIn('buildmode', [s for s, _ in help_of('go.txt')['subs']])
        self.assertEqual(opt(help_of('dms.txt'), '-c')['l'], ['config'])

    def test_go_flag_styles(self):
        gofmt = help_of('gofmt.txt')
        self.assertEqual(opt(gofmt, '-cpuprofile')['arg'], 'string')
        self.assertTrue(opt(gofmt, '-cpuprofile')['file'])
        self.assertIsNone(opt(gofmt, '-d')['arg'])
        self.assertIn('display diffs', opt(gofmt, '-d')['d'])
        clip = help_of('cliphist.txt')
        self.assertEqual(opt(clip, '-db-path')['o'], ['db-path'])
        self.assertTrue(opt(clip, '-db-path')['file'])
        self.assertEqual(opt(clip, '-max-items')['arg'], 'max-items')
        lg = help_of('lazygit.txt')
        self.assertEqual(opt(lg, '--help')['s'], ['h'])
        self.assertEqual(opt(lg, '--print-config-dir')['o'], ['cd'])
        sh = help_of('shfmt.txt')
        self.assertEqual(opt(sh, '-ln')['vals'], ['bash', 'posix', 'mksh', 'bats', 'zsh'])
        self.assertEqual((opt(sh, '--list')['arg'], opt(sh, '--list')['opt']), ('0', True))
        self.assertEqual(opt(sh, '--write')['s'], ['w'])

    def test_misc_styles(self):
        curl = help_of('curl.txt')
        self.assertTrue(opt(curl, '--output')['file'])
        self.assertEqual(curl['usage'], [[('arg', 'url')]])
        http = help_of('http.server.txt')
        self.assertTrue(opt(http, '--directory')['file'])
        self.assertEqual(opt(http, '-b')['arg'], 'ADDRESS')
        grim = help_of('grim.txt')
        self.assertEqual(opt(grim, '-t')['vals'], ['png', 'ppm', 'jpeg'])
        self.assertEqual(opt(grim, '-s')['arg'], 'factor')
        self.assertEqual(opt(help_of('slurp.txt'), '-a')['arg'], 'w:h')
        rg = help_of('rg.txt')
        self.assertEqual(opt(rg, '--regexp')['arg'], 'PATTERN')
        self.assertEqual(opt(rg, '--hidden')['s'], ['.'])
        self.assertEqual(rg['usage'], [[('arg', 'PATTERN')]])
        tar = help_of('tar.txt')
        self.assertEqual(opt(tar, '-A')['l'], ['catenate', 'concatenate'])

    def test_man_page(self):
        info = help_of('grim.man.txt')
        self.assertEqual(info['usage'], [[]])
        self.assertEqual(opt(info, '-t')['vals'], ['png', 'jpeg', 'ppm'])
        self.assertEqual(opt(info, '-g')['arg'], '<x>,<y> <width>x<height>')
        self.assertNotIn('If set to -', opt(info, '-g')['d'])
        self.assertEqual(len(info['opts']), 9)

    def test_escape_sequences_and_overstrike_are_dropped(self):
        info = params.parse_help('  -x, --\x1b[1mexec\x1b[0m  r\bru\bun\x07 it\x1b]2;pwn\x07\n')
        o = opt(info, '--exec')
        self.assertEqual(o['d'], 'run it')


class Sources(unittest.TestCase):
    def test_fish_argparse(self):
        info = params.parse_fish_source((FIX / 'src' / 'upscale.fish').read_text(), 'upscale')
        names = [sorted(params._names(o)) for o in info['opts']]
        self.assertIn(['--help', '-h'], names)
        self.assertIn(['--noise'], names)
        self.assertIn(['--dry-run'], names)
        self.assertIn(['--jobs', '-j'], names)
        self.assertNotIn(['--zzz', '-z'], names)
        o = opt(info, '--output')
        self.assertEqual((o['arg'], o['file']), ('output', True))
        self.assertIsNone(opt(info, '--verbose')['arg'])

    def test_fish_switch(self):
        info = params.parse_fish_source((FIX / 'src' / 'svc.fish').read_text(), 'svc')
        self.assertEqual([s for s, _ in info['subs']], ['start', 'stop', 'status'])
        self.assertEqual(params._names(opt(info, '-h')), {'-h', '--help'})
        self.assertEqual(opt(info, '--log')['arg'], 'log')

    def test_sh(self):
        info = params.parse_sh_source((FIX / 'src' / 'backup.sh').read_text())
        d = opt(info, '-d')
        self.assertEqual((d['l'], d['arg'], d['req']), (['dest'], 'DEST', True))
        self.assertEqual(opt(info, '--exclude')['arg'], 'exclude')
        self.assertIsNone(opt(info, '--dry-run')['arg'])
        self.assertEqual(opt(info, '--mode')['arg'], 'mode')
        self.assertIsNone(opt(info, '-q')['arg'])
        self.assertEqual([s for s, _ in info['subs']], ['push', 'pull'])
        self.assertEqual(info['usage'], [[('flag', '-d', 'DEST'), ('arg', 'SOURCE')]])

    def test_python_argparse(self):
        info = params.parse_py_source((FIX / 'src' / 'tagger.py').read_text())
        i = opt(info, '--input')
        self.assertEqual((i['req'], i['file']), (True, True))
        self.assertEqual(opt(info, '--format')['vals'], ['jpg', 'png', 'webp'])
        self.assertIsNone(opt(info, '--verbose')['arg'])
        self.assertIn('no-color', opt(info, '--color')['l'])
        self.assertEqual(opt(info, '-n')['arg'], 'N')
        self.assertEqual(info['subs'], [('scan', 'scan folders'), ('export', 'export tags')])
        self.assertEqual(info['usage'], [[('arg', 'PHOTO')]])
        self.assertEqual(params.parse_py_source('def broken(:\n'), {})


class Completions(Isolated):
    HOSTILE = {'opts': [
        {'s': ['m'], 'l': ['mode'], 'o': [], 'arg': 'M', 'opt': False, 'file': False, 'req': True,
         'vals': ['ok', '$(touch PWNED)', "it's", 'a b', '(x)', '~root', '*'], 'd': "it's a \\ $(touch PWNED) test"},
        {'s': [], 'l': ['out'], 'o': [], 'arg': 'FILE', 'opt': False, 'file': True, 'req': False, 'vals': [],
         'd': ''},
    ], 'subs': [('serve', 'run $HOME'), ('$(bad)', '')], 'src': 'man'}

    def test_render_quotes_everything(self):
        text = params.render_completions('my-cmd', self.HOSTILE)
        self.assertIn("complete -c 'my-cmd' -s 'm' -l 'mode' -x -a 'ok' -d '(required) it\\'s a \\\\ "
                      "$(touch PWNED) test'", text)
        self.assertIn("complete -c 'my-cmd' -l 'out' -r -F", text)
        self.assertIn("-n '__fish_use_subcommand' -a 'serve' -d 'run $HOME'", text)
        self.assertNotIn('bad', text)

    @unittest.skipUnless(FISH, 'fish not installed')
    def test_fish_loads_it_literally(self):
        f = self.tmp / 'c.fish'
        f.write_text(params.render_completions('my-cmd', self.HOSTILE))
        r = subprocess.run([FISH, '--no-config', '-c',
                            'source $argv[1]; complete -C "my-cmd --mode "; complete -C "my-cmd -"', str(f)],
                           cwd=self.tmp, capture_output=True, text=True, env={**os.environ, 'HOME': str(self.tmp)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('ok\t(required) it\'s a \\ $(touch PWNED) test', r.stdout)
        self.assertFalse((self.tmp / 'PWNED').exists())

    def test_compgen_runs_help_once_and_caches(self):
        if not shutil.which('true'):
            self.skipTest('no coreutils true')
        bindir = self.tmp / 'bin'
        bindir.mkdir()
        exe = bindir / 'fsfaketrue'
        shutil.copy(os.path.realpath(shutil.which('true')), exe)
        with mock.patch.dict(os.environ, {'PATH': f'{bindir}:{os.environ["PATH"]}'}):
            out = params.compgen('fsfaketrue', 'file', str(exe))
            self.assertEqual(out, common.cache_dir() / 'completions' / 'fsfaketrue.fish')
            text = out.read_text()
            self.assertIn("complete -c 'fsfaketrue' -l 'help'", text)
            self.assertIn("-l 'version'", text)
            with mock.patch.object(params, '_run_help', side_effect=AssertionError('ran again')):
                self.assertEqual(params.compgen('fsfaketrue', 'file', str(exe)), out)
            os.utime(exe, ns=(1, 1))
            with mock.patch.object(params, '_run_help', return_value={}) as run:
                params.compgen('fsfaketrue', 'file', str(exe))
                run.assert_called_once()

    def test_compgen_skips_what_fish_ships(self):
        with mock.patch.object(params, '_fish_ships', return_value=True), \
                mock.patch.object(params, 'help_info', side_effect=AssertionError('parsed')):
            self.assertIsNone(params.compgen('tar', 'file', '/usr/bin/tar'))

    def test_function_and_cli(self):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(params, '_fish_ships', return_value=False):
            params.main('compgen', ['--cmd-name', 'upscale', '--cmd-kind', 'function',
                                    '--cmd-path', str(FIX / 'src' / 'upscale.fish')])
        kind, payload = buf.getvalue().strip().split('\t')
        self.assertEqual(kind, 'compgen')
        self.assertIn("-s 'o' -l 'output' -r -F", Path(common.dec(payload)).read_text())


class ExecSafety(Isolated):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        self.path = mock.patch.dict(os.environ, {'PATH': f'{self.bin}:/usr/bin'})
        self.path.start()
        self.elf = shutil.which('true', path='/usr/bin')

    def tearDown(self):
        self.path.stop()
        super().tearDown()

    def put(self, name, src=None, text=None, mode=0o755):
        p = self.bin / name
        if text is not None:
            p.write_text(text)
        else:
            shutil.copy(src, p)
        p.chmod(mode)
        return str(p)

    def test_rules(self):
        self.assertTrue(params.exec_ok(self.put('okbin', self.elf), 'okbin'))
        self.assertFalse(params.exec_ok(self.put('script', text='#!/bin/sh\necho hi\n'), 'script'))
        self.assertFalse(params.exec_ok(self.put('reboot', self.elf), 'reboot'))
        self.assertFalse(params.exec_ok(self.put('mkfs.ext4', self.elf), 'mkfs.ext4'))
        self.assertFalse(params.exec_ok(self.put('killer', self.elf), 'killer'))
        self.assertFalse(params.exec_ok(self.put('suid', self.elf, mode=0o4755), 'suid'))
        os.symlink(self.put('systemctl', self.elf), self.bin / 'innocent')
        self.assertFalse(params.exec_ok(str(self.bin / 'innocent'), 'innocent'))
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir()
        shutil.copy(self.elf, elsewhere / 'okbin')
        self.assertFalse(params.exec_ok(str(elsewhere / 'okbin'), 'okbin'))

    def test_binary_that_never_mentions_help(self):
        self.assertFalse(params.exec_ok(self.put('streamer', text='\x7fELF\0open_port\0helpers14small\0'), 'streamer'))
        self.assertTrue(params.exec_ok(self.put('tool', text='\x7fELF\0  -h, --help  show this\0'), 'tool'))
        self.assertTrue(params.exec_ok(self.put('gotool', text='\x7fELF\0Usage of %s:\0'), 'gotool'))
        self.assertFalse(params.exec_ok(self.put('empty', text=''), 'empty'))

    def test_scripts_are_never_executed(self):
        marker = self.tmp / 'RAN'
        p = self.put('tool.sh', text=f'#!/bin/sh\ntouch {marker}\ncase "$1" in\n  -x|--xx) shift 2 ;;\nesac\n')
        info = params.help_info('tool.sh', p, 'file', allow_exec=True)
        self.assertEqual(opt(info, '--xx')['arg'], 'xx')
        self.assertFalse(marker.exists())

    def test_help_run_is_isolated(self):
        if not self.elf:
            self.skipTest('no true')
        p = self.put('okbin', self.elf)
        seen = {}

        real_popen = subprocess.Popen

        def fake_popen(argv, **kw):
            seen.update(kw, argv=argv, listing=os.listdir(kw['cwd']))
            return real_popen(['true'], start_new_session=True)

        with mock.patch('subprocess.Popen', side_effect=fake_popen):
            params._run_help(p)
        self.assertEqual(seen['argv'][1:], ['-h'])
        self.assertEqual(seen['stdin'], subprocess.DEVNULL)
        self.assertEqual(seen['listing'], [])
        self.assertEqual(seen['env']['LANG'], 'C')
        self.assertEqual(seen['env']['HOME'], seen['cwd'])


    def test_hanging_help_is_killed_once(self):
        p = self.put('slowbin', self.elf)
        calls = []
        real_popen = subprocess.Popen

        def fake_popen(argv, **kw):
            calls.append(argv[1])
            return real_popen(['sleep', '30'], start_new_session=True)

        with mock.patch('subprocess.Popen', side_effect=fake_popen), mock.patch.object(params, 'HELP_TIMEOUT', 0.2):
            t = time.monotonic()
            self.assertEqual(params._run_help(p), {'opts': [], 'usage': [], 'subs': []})
            self.assertLess(time.monotonic() - t, 2)
        self.assertEqual(calls, ['--help'])


class Tldr(Isolated):
    def test_parse_and_render(self):
        ex = params.parse_tldr((FIX / 'tldr' / 'en' / 'common' / 'git-commit.md').read_text())
        self.assertEqual(ex[1], ('Commit staged files to the repository with the specified message',
                                 'git commit {{[-m|--message]}} "{{message}}"'))
        self.assertEqual(params._render('{{[-c|--create]}}'), '-c')
        self.assertEqual(params._render('"{{message}}"'), '"‹message›"')
        self.assertEqual(params._render('{{path/to/file[.gz|.bz2]}}'), '‹path/to/file[.gz|.bz2]›')

    def test_match(self):
        cmd = 'tar xf {{path/to/file.tar}} {{[-C|--directory]}} {{path/to/directory}}'
        self.assertEqual(params._tldr_match(['tar', 'xf', 'a.tar'], cmd), '-C ‹path/to/directory›')
        self.assertEqual(params._tldr_match(['tar', 'xf', 'a.tar', '--directory'], cmd), '‹path/to/directory›')
        self.assertIsNone(params._tldr_match(['tar', 'cf'], cmd))
        self.assertIsNone(params._tldr_match(['tar', 'xf', 'a', '-C', 'd'], cmd))
        self.assertEqual(params._tldr_match(['pacman', '-S'], 'sudo pacman -S {{package}}'), '‹package›')

    def test_index_and_static_items(self):
        self.install_tldr()
        items = params.static_items({'tar', 'ffmpeg', 'htop', 'ip', 'not-installed'})
        self.assertIn('tar cf ‹path/to/target.tar› ‹path/to/file1 path/to/file2 ...›', items)
        self.assertIn('htop -u ‹username›', items)
        self.assertIn('ip l s ‹ethX› ‹up|down›', items)
        self.seed_help('fakeup', 'waifu2x-ncnn-vulkan.txt')
        self.assertIn('fakeup -i ‹infile› -o ‹outfile›', params.static_items({'fakeup'}))
        self.assertEqual(params.static_items({'nothing'}), [])

    def test_update_extracts_only_safe_pages(self):
        def zipped(pages):
            b = io.BytesIO()
            with zipfile.ZipFile(b, 'w') as z:
                for name, text in pages.items():
                    z.writestr(name, text)
            return b.getvalue()

        page = '# foo\n\n- Do it:\n\n`foo {{bar}}`\n'
        bodies = {lang: zipped({'common/foo.md': page, 'linux/lin.md': page.replace('foo', 'lin'),
                                'osx/mac.md': page, 'common/../evil.md': page, '../evil.md': page,
                                'linux/gnu[.md': page, 'index.json': '{}'}) for lang in params.LANGS}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(url, timeout):
            self.assertTrue(url.startswith('https://github.com/tldr-pages/tldr/releases/latest/download/'))
            return Resp(bodies[url.rsplit('.', 2)[1]])

        old = params.tldr_dir() / 'en' / 'common' / 'stale.md'
        old.parent.mkdir(parents=True)
        old.write_text(page)
        buf = io.StringIO()
        with mock.patch('urllib.request.urlopen', urlopen), redirect_stdout(buf):
            params.main('tldr-update', [])
        self.assertEqual(buf.getvalue(), 'ok\t2\n')
        files = sorted(str(p.relative_to(params.tldr_dir())) for p in params.tldr_dir().rglob('*.md'))
        self.assertEqual(files, ['en/common/foo.md', 'en/linux/lin.md'])
        self.assertFalse((self.tmp / 'xdg_cache_home' / 'evil.md').exists())
        self.assertEqual(params.static_items({'foo'}), ['foo ‹bar›'])

    def test_failed_download_keeps_old_cache(self):
        self.install_tldr()
        with mock.patch('urllib.request.urlopen', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                params.tldr_update()
        self.assertTrue((params.tldr_dir() / 'en' / 'common' / 'tar.md').exists())
        self.assertEqual([p.name for p in params.tldr_dir().parent.iterdir() if p.name.startswith('.tldr')], [])

    def test_doctor(self):
        self.assertIn('fishsense update', params.doctor_lines()[0])
        self.install_tldr()
        lines = params.doctor_lines()
        self.assertTrue(lines[0].startswith('tldr: 5 English pages (updated '), lines[0])
        self.assertEqual(lines[1], 'generated completions: 0')
        idx = params.tldr_dir() / 'index.json'
        idx.write_text(idx.read_text().replace('"v": 2', '"v": 1'))
        self.assertIn('fishsense update', params.doctor_lines()[0])
        self.assertEqual(params.static_items({'tar'}), [])


class Next(Isolated):
    HIST = ['fakeup -i a.png -o a2.png -s 2', 'ls', 'fakeup -i b.png -o b2.png', 'cd /tmp; fakeup -f png -i c.png']

    def setUp(self):
        super().setUp()
        self.install_tldr()
        self.seed_help('fakeup', 'waifu2x-ncnn-vulkan.txt')

    def test_habit_flags_first(self):
        self.assertEqual(params.next('fakeup -i new.png ', '/', self.HIST), 'fakeup -i new.png -o ‹output-path›')

    def test_habit_flags_need_two_sightings(self):
        self.assertIsNone(params.next('fakeup -i x -o y ', '/', self.HIST))
        hist = self.HIST + ['fakeup -i d.png -o d2.png -s 4']
        self.assertEqual(params.next('fakeup -i x -o y ', '/', hist), 'fakeup -i x -o y -s ‹scale›')

    def test_no_flag_while_a_value_is_pending(self):
        self.assertEqual(params.next('fakeup -o z -i ', '/', self.HIST), 'fakeup -o z -i ‹input-path›')
        self.assertIsNone(params._habit_flag(['fakeup', '-i'], self.HIST, {}))

    def test_one_letter_command(self):
        hist = ['s -x a', 'echo sss; s -x b', '/usr/bin/s -q c', 'ls -s', 'sudo s -x d']
        self.assertEqual(params._habit_flag(['s'], hist, {}), '-x')
        self.assertIsNone(params._habit_flag(['s'], ['ls -s -y', 'yes s -y'], {}))

    def test_value_placeholder_after_flag(self):
        self.assertEqual(params.next('fakeup -i ', '/', []), 'fakeup -i ‹input-path›')

    def test_usage_required_parts(self):
        self.assertEqual(params.next('fakeup ', '/', []), 'fakeup -i ‹infile› -o ‹outfile›')
        self.assertEqual(params.next('fakeup -v -i a ', '/', []), 'fakeup -v -i a -o ‹outfile›')

    def test_tldr(self):
        self.assertEqual(params.next('git commit ', '/', []), 'git commit -m "‹message›"')
        self.assertEqual(params.next('sudo tar ', '/', []),
                         'sudo tar cf ‹path/to/target.tar› ‹path/to/file1 path/to/file2 ...›')
        self.assertEqual(params.next('ffmpeg -i in.mkv ', '/', []), 'ffmpeg -i in.mkv -vn ‹path/to/sound.mp3›')
        self.assertEqual(params.next('ip address ', '/', []),
                         'ip address ‹add|delete› ‹ip_address›/‹mask› dev ‹ethX›')

    def test_no_tldr_for_fish_builtins(self):
        page = params.tldr_dir() / 'en' / 'common' / 'zqhist.md'
        page.write_text('# zqhist\n\n- Show the last entries:\n\n`zqhist {{20}}`\n')
        self.assertIsNone(params.next('zqhist ', '/', []))
        bindir = self.tmp / 'bin'
        bindir.mkdir()
        (bindir / 'zqhist').write_text('#!/bin/sh\n')
        (bindir / 'zqhist').chmod(0o755)
        with mock.patch.dict(os.environ, {'PATH': f'{bindir}:{os.environ["PATH"]}'}):
            self.assertEqual(params.next('zqhist ', '/', []), 'zqhist ‹20›')

    def test_native_history_wins(self):
        self.assertIsNone(params.next('fakeup -i a.png ', '/', self.HIST))
        self.assertIsNone(params.next('git commit ', '/', ['git commit -m "x"']))

    def test_always_extends_the_buffer(self):
        bufs = ['tar ', 'tar  ', 'echo "a b" | tar ', 'git commit -m ', 'fakeup -i "a b.png" ', 'x; ffmpeg -i ',
                'tar "open ', 'tar', '', ' ', 'nope ', 'fakeup -i a\n', 'fakeup -i ‹ ', 'FOO=1 tar ']
        for b in bufs:
            r = params.next(b, '/', self.HIST)
            if r is not None:
                self.assertTrue(r.startswith(b) and len(r) > len(b), (b, r))
                self.assertNotIn('\n', r[len(b):])
        self.assertIsNone(params.next('tar "open ', '/', []))
        self.assertIsNone(params.next('tar', '/', []))

    def test_cli_print_and_notify(self):
        fish_data = self.tmp / 'fishdata'
        fish_data.mkdir()
        (fish_data / 'fish_history').write_text(common.format_fish_history([(h, ()) for h in self.HIST]))
        args = ['--pid', str(os.getpid()), '--id', '7', '--cwd', common.enc('/tmp'),
                '--buffer', common.enc('fakeup -i new.png '), '--fish-data', str(fish_data), '--real-name', 'fish']
        buf = io.StringIO()
        with redirect_stdout(buf):
            params.main('next', args)
        self.assertEqual(buf.getvalue(), f'ghost\t{common.enc("fakeup -i new.png -o ‹output-path›")}\n')

        got = []
        res = common.run_dir() / f'{os.getpid()}.result'

        def take(*a):
            got.append(res.read_text())
            res.write_text('')
        old = signal.signal(signal.SIGUSR1, take)
        (common.run_dir() / f'{os.getpid()}.rank').write_text('1\n')
        try:
            params.main('next', args + ['--notify'])
        finally:
            signal.signal(signal.SIGUSR1, old)
        self.assertEqual(got, [f'ghost\t7\t{common.enc("fakeup -i new.png -o ‹output-path›")}\n', ''])

    def test_result_slot_is_free_once_emptied(self):
        res = common.run_dir() / f'{os.getpid()}.result'
        res.write_text('')
        (common.run_dir() / f'{os.getpid()}.rank').write_text('1\n')
        got = []

        def take(*a):
            got.append(res.read_text())
            res.write_text('')
        old = signal.signal(signal.SIGUSR1, take)
        try:
            t = time.monotonic()
            params._notify(os.getpid(), 'ghost', '1', 'x')
            self.assertLess(time.monotonic() - t, 0.5)
            self.assertEqual(got[0], 'ghost\t1\tx\n')
        finally:
            signal.signal(signal.SIGUSR1, old)

    def test_wraps_uses_the_wrapped_function(self):
        with mock.patch.object(params, '_fish_ships', return_value=False):
            out = params.compgen('upscale', 'wraps', str(FIX / 'src' / 'upscale.fish'))
        self.assertIn("-l 'scale' -r", out.read_text())

    def test_cli_compgen_after_ghost(self):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(params, '_fish_ships', return_value=False):
            params.main('next', ['--pid', '1', '--id', '1', '--cwd', common.enc('/'),
                                 '--buffer', common.enc('upscale -o '), '--no-completions', '--cmd-name', 'upscale',
                                 '--cmd-kind', 'function', '--cmd-path', str(FIX / 'src' / 'upscale.fish')])
        kinds = [l.split('\t')[0] for l in buf.getvalue().splitlines()]
        self.assertEqual(kinds, ['ghost', 'compgen'])
        self.assertIn(common.enc('upscale -o ‹output›'), buf.getvalue())


if __name__ == '__main__':
    unittest.main()
