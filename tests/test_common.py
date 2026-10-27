import os
import resource
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'conf.d' / 'fishsense'))
import common


class HistoryRoundTrip(unittest.TestCase):
    def test_escape_roundtrip(self):
        for s in ['echo hi', 'a\\nb', 'for i in 1\n  echo $i\nend', 'x\\\\y', 'ends with \\']:
            self.assertEqual(common.unescape_hist(common.escape_hist(s)), s)

    def test_read_write(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'x_history'
            p.write_text(common.format_fish_history(
                [('cd etc/', ('etc/',)), ('printf "a\\nb"\necho two', ())], when=100))
            items = common.read_fish_history(p)
            self.assertEqual([i.cmd for i in items], ['cd etc/', 'printf "a\\nb"\necho two'])
            self.assertEqual(items[0].paths, ('etc/',))
            self.assertEqual(items[0].when, 100)

    def test_real_format(self):
        text = ('- cmd: yay -Syyu\n  when: 1791022541\n- cmd: ls /\n  when: 1791022542\n'
                '  paths:\n    - /\n')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'fish_history'
            p.write_text(text)
            self.assertEqual(common.read_fish_history(p),
                             [common.HistItem('yay -Syyu', 1791022541, ()),
                              common.HistItem('ls /', 1791022542, ('/',))])


    def test_raw_bytes_and_odd_separators(self):
        cmds = [b'cat caf\xe9.txt', 'echo one\u2028two'.encode(), b'a\rb\x0bc\x1cd\x85e']
        with tempfile.TemporaryDirectory() as d:
            p, out = Path(d) / 'fish_history', Path(d) / 'shadow_history'
            p.write_bytes(b''.join(b'- cmd: ' + c + b'\n  when: 1\n' for c in cmds))
            items = common.read_fish_history(p)
            self.assertEqual(len(items), 3)
            common.write_atomic(out, common.format_fish_history([(i.cmd, ()) for i in items], when=1))
            self.assertEqual(out.read_bytes(), p.read_bytes())
            self.assertEqual(common.dec(common.enc(items[0].cmd)), items[0].cmd)


class LogAndMisc(unittest.TestCase):
    def test_log_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'log.tsv'
            recs = [common.LogRec(1, 2, 0, '/tmp/a b', 'echo "tab\there"\nx')]
            common.write_log(recs, p)
            self.assertEqual(common.read_log(p), recs)

    def test_sanitize(self):
        self.assertEqual(common.sanitize('a\x1b]2;x\x07b\x9bc'), 'a]2;xbc')
        self.assertEqual(common.sanitize('a\nb', keep_newlines=True), 'a\nb')

    def test_write_atomic_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'x_history'
            p.write_text('old')
            common.write_atomic(p, 'new', ok=lambda: False)
            self.assertEqual((p.read_text(), os.listdir(d)), ('old', ['x_history']))
            code = f'import sys; sys.path.insert(0, {str(Path(common.__file__).parent)!r}); import common\n' \
                   f'common.write_atomic({str(p)!r}, "x" * 200000)'
            r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                               preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536)))
            self.assertIn('File too large', r.stderr)
            self.assertEqual((p.read_text(), os.listdir(d)), ('old', ['x_history']))

    def test_sigterm_mid_write_leaves_no_copy(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'log.tsv'
            p.write_text('old')
            code = (f'import os, signal, sys; sys.path.insert(0, {str(Path(common.__file__).parent)!r})\n'
                    'import fishsense, common\n'
                    'from pathlib import Path\n'
                    'w = Path.write_text\n'
                    'def write(self, *a, **k):\n'
                    '    n = w(self, *a, **k)\n'
                    '    os.kill(os.getpid(), signal.SIGTERM)\n'
                    '    return n\n'
                    'Path.write_text = write\n'
                    f'common.write_atomic({str(p)!r}, "new")\n')
            r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
            self.assertEqual((r.returncode, r.stderr), (143, ''))
            self.assertEqual((p.read_text(), os.listdir(d)), ('old', ['log.tsv']))

    def test_gc(self):
        with tempfile.TemporaryDirectory() as d:
            live = Path(d) / f'fishsense_{os.getpid()}_history'
            dead = Path(d) / 'fishsense_999999999_history'
            live.touch()
            dead.touch()
            common.gc_shadows(d)
            self.assertTrue(live.exists())
            self.assertFalse(dead.exists())


if __name__ == '__main__':
    unittest.main()
