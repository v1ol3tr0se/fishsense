import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / 'conf.d' / 'fishsense'
sys.path.insert(0, str(HERE))
import common
import rank

NOW = 1_800_000_000
DAY = 86400
H = common.HistItem
APP, HOME = '/home/u/Projects/app', '/home/u'


def rec(cmd, days_ago, cwd=HOME, pid=1, status=0):
    return common.LogRec(int(NOW - days_ago * DAY), pid, status, cwd, cmd)


def suggest(order, prefix):
    """What fish shows: the newest (last) item starting with the typed prefix."""
    return next((c for c, _ in reversed(order) if c.startswith(prefix)), None)


class Scoring(unittest.TestCase):
    def test_cwd_beats_global_recency(self):
        hist = [H('uftrace record', NOW - 10 * DAY, ()), H('ls', NOW - 5 * DAY, ()),
                H('ufw allow 22', NOW - DAY, ())]
        log = [rec('uftrace record', 10 + i / 10, APP) for i in range(4)]
        log += [rec('ufw allow 22', 1 + i / 10, HOME) for i in range(2)] + [rec('ls', 5)]
        log.sort(key=lambda r: r.when)
        self.assertEqual(suggest(rank.ranked(hist, log, APP, 'cd ~/Projects/app', NOW), 'uf'),
                         'uftrace record')
        self.assertEqual(suggest(rank.ranked(hist, log, '/etc', '', NOW), 'uf'), 'ufw allow 22')

    def test_transition(self):
        repo = '/home/u/repo'
        hist, log, t = [], [], 30.0
        for i, msg in enumerate(['one', 'two', 'three']):
            pid = 100 + i
            for cmd, p in [('git add .', pid), ('ls', 999), (f'git commit -m "{msg}"', pid)]:
                t -= 0.01
                hist.append(H(cmd, int(NOW - t * DAY), ()))
                log.append(rec(cmd, t, repo, p))
        for i in range(12):
            t -= 0.5
            hist.append(H('git status', int(NOW - t * DAY), ()))
            log.append(rec('git status', t, repo, 200))
        hist.append(H('git checkout main', NOW - 60, ()))
        log.append(rec('git checkout main', 60 / DAY, repo, 200))

        self.assertEqual(suggest(rank.ranked(hist, log, repo, 'git add .', NOW), 'git '),
                         'git commit -m "three"')
        self.assertEqual(suggest(rank.ranked(hist, log, repo, 'git add src/x.c', NOW), 'git '),
                         'git commit -m "three"')
        self.assertNotIn('commit', suggest(rank.ranked(hist, log, repo, 'ls', NOW), 'git '))

    def test_failures_sink(self):
        def order(status):
            hist = [H('make flash', NOW - 2 * DAY, ()), H('make flsah', NOW - DAY, ())]
            log = [rec('make flash', 2, APP), rec('make flsah', 1, APP, status=status)]
            return rank.ranked(hist, log, APP, '', NOW)
        self.assertEqual(suggest(order(2), 'make fl'), 'make flash')
        self.assertEqual(suggest(order(0), 'make fl'), 'make flsah')
        self.assertEqual(suggest(order(130), 'make fl'), 'make flsah')

    def test_no_context_is_recency(self):
        hist = [H(c, NOW - (10 - i) * DAY, ()) for i, c in enumerate(['b', 'a', 'c', 'a', 'd'])]
        self.assertEqual([c for c, _ in rank.ranked(hist, [], '/x', '', NOW)], ['b', 'c', 'a', 'd'])

    def test_dedupe_paths_and_leading_space(self):
        hist = [H('cat notes.txt', NOW - 3, ('notes.txt',)), H(' secret', NOW - 2, ()),
                H('cat notes.txt', NOW - 1, ())]
        self.assertEqual(rank.ranked(hist, [], '/x', '', NOW), [('cat notes.txt', ('notes.txt',))])


    def test_static_items(self):
        fake = types.SimpleNamespace(static_items=lambda names: ['tar ‹-xvf ‹file.tar››', 'ls\x1b[2J', 'ls'])
        with mock.patch.dict(sys.modules, params=fake):
            self.assertEqual(rank._static({'ls'}), [('tar ‹-xvf ‹file.tar››', ()), ('ls[2J', ())])
        with mock.patch.dict(sys.modules, params=types.SimpleNamespace()):
            self.assertEqual(rank._static(set()), [])


class Cli(unittest.TestCase):
    """Runs the real helper the way fish does, with every XDG dir in a temp tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.env = dict(os.environ, HOME=str(d / 'home'), XDG_CONFIG_HOME=str(d / 'config'),
                        XDG_DATA_HOME=str(d / 'data'), XDG_CACHE_HOME=str(d / 'cache'),
                        XDG_RUNTIME_DIR=str(d / 'run'), FISHSENSE_DEBUG='1',
                        PYTHONPYCACHEPREFIX=str(d / 'cache' / 'pycache'))
        self.env.pop('PYTHONDONTWRITEBYTECODE', None)
        self.fish_data = d / 'data' / 'fish'
        self.fish_data.mkdir(parents=True)
        self.log = d / 'data' / 'fishsense' / 'log.tsv'
        self.pid = 4242

    def write(self, hist, log):
        (self.fish_data / 'fish_history').write_text(common.format_fish_history(hist, when=NOW))
        common.write_log(log, self.log)

    def run_rank(self, *extra, pid=None, cwd='/x', last=''):
        t = time.perf_counter()
        p = subprocess.run([sys.executable, str(HERE / 'fishsense.py'), 'rank',
                            '--pid', str(pid or self.pid), '--fish-data', str(self.fish_data),
                            '--real-name', 'fish', '--cwd', common.enc(cwd),
                            '--last', *([common.enc(last)] if last else []), *extra],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, '', ''))
        return time.perf_counter() - t

    def shadow(self, pid=None):
        return common.read_fish_history(self.fish_data / f'fishsense_{pid or self.pid}_history')

    def test_privacy_pruning_and_paths(self):
        old = time.time() - 30 * DAY
        self.write([('cat notes.txt', ('notes.txt',)), ('printf a\\\\nb\necho two', ()), ('ls', ())],
                   [common.LogRec(int(old), 1, 0, '/x', c)
                    for c in ['ls', 'rm -rf deleted-from-history', ' ls', 'cat notes.txt']]
                   + [common.LogRec(int(time.time()), 1, 0, '/x', 'just ran, not saved yet')])
        self.run_rank()
        self.assertEqual([r.cmd for r in common.read_log(self.log)],
                         ['ls', 'cat notes.txt', 'just ran, not saved yet'])
        sh = self.shadow()
        self.assertEqual({i.cmd for i in sh}, {'cat notes.txt', 'printf a\\\\nb\necho two', 'ls'})
        self.assertEqual(next(i for i in sh if i.cmd == 'cat notes.txt').paths, ('notes.txt',))
        self.assertLess(time.time() - sh[0].when, 60)
        self.assertEqual(os.stat(self.log).st_mode & 0o777, 0o600)

        ino = os.stat(self.log).st_ino
        self.run_rank()
        self.assertEqual(os.stat(self.log).st_ino, ino)

    def test_cleared_history_clears_the_log(self):
        self.write([], [common.LogRec(1, 1, 0, '/x', 'ls'),
                        common.LogRec(int(time.time()) - 5, 1, 0, '/x', 'echo api-key-sk-e2e')])
        (self.fish_data / 'fish_history').unlink()
        self.run_rank()
        self.assertEqual(common.read_log(self.log), [])

    def test_unreadable_history_keeps_log(self):
        self.write([('ls', ())], [common.LogRec(1, 1, 0, '/x', 'gone')])
        (self.fish_data / 'fish_history').chmod(0)
        self.addCleanup((self.fish_data / 'fish_history').chmod, 0o600)
        if os.access(self.fish_data / 'fish_history', os.R_OK):
            self.skipTest('root reads anything')
        self.run_rank()
        self.assertEqual(len(common.read_log(self.log)), 1)

    def test_named_session_keeps_the_others(self):
        old = int(time.time()) - 30 * DAY
        self.write([('fakeapp launch', ()), ('ls', ())],
                   [common.LogRec(old, 1, 0, '/x', c) for c in ('fakeapp launch', 'ls', 'make work', 'gone')])
        (self.fish_data / 'work_history').write_text(common.format_fish_history([('make work', ())], when=NOW))
        (self.fish_data / 'scratch_history').write_text(common.format_fish_history([('echo s', ())], when=NOW))
        (self.fish_data / f'fishsense_{self.pid + 1}_history').write_text(
            common.format_fish_history([('gone', ())], when=NOW))
        p = subprocess.run([sys.executable, str(HERE / 'fishsense.py'), 'rank', '--pid', str(self.pid),
                            '--fish-data', str(self.fish_data), '--real-name', 'scratch', '--cwd', common.enc('/x')],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stderr), (0, ''))
        self.assertEqual([r.cmd for r in common.read_log(self.log)], ['fakeapp launch', 'ls', 'make work'])

    def test_appends_the_record_quietly(self):
        self.write([('ls', ())], [])
        self.run_rank('--status', '1', '--log-cwd', common.enc('/a b'), '--log-cmd', common.enc('ls -x'))
        (r,) = common.read_log(self.log)
        self.assertEqual((r.pid, r.status, r.cwd, r.cmd), (self.pid, 1, '/a b', 'ls -x'))
        self.assertLess(abs(r.when - time.time()), 5)
        self.log.chmod(0o444)
        if os.access(self.log, os.W_OK):
            self.skipTest('root writes anything')
        self.run_rank('--status', '0', '--log-cwd', common.enc('/'), '--log-cmd', common.enc('ls'))
        self.assertEqual(len(common.read_log(self.log)), 1)

    def test_compacts_log(self):
        self.write([('ls', ())], [common.LogRec(i, 1, 0, '/x', 'ls') for i in range(rank.LOG_MAX + 7)])
        ino = os.stat(self.log).st_ino
        self.run_rank()
        self.assertEqual(os.stat(self.log).st_ino, ino)
        n = rank.LOG_MAX + rank.LOG_SLACK + 7
        self.write([('ls', ())], [common.LogRec(i, 1, 0, '/x', 'ls') for i in range(n)])
        self.run_rank()
        recs = common.read_log(self.log)
        self.assertEqual((len(recs), recs[0].when), (rank.LOG_MAX, n - rank.LOG_MAX))

    def test_append_during_rewrite_survives(self):
        old = int(time.time()) - 30 * DAY
        self.write([('ls', ())], [common.LogRec(old, 1, 0, '/x', c) for c in ('ls', 'gone')])
        write_text = Path.write_text

        def write_then_append(path, *a, **k):
            n = write_text(path, *a, **k)
            if path.name.endswith('.tmp'):
                with open(self.log, 'a') as f:
                    f.write(f'{old}\t2\t0\t%2Fx\tls\n')
            return n
        with mock.patch.object(common, 'log_path', return_value=self.log), \
                mock.patch.object(Path, 'write_text', write_then_append):
            rank.load_log([common.HistItem('ls', 0, ())], time.time())
        self.assertEqual([r.pid for r in common.read_log(self.log)], [1, 1, 2])
        self.assertEqual(list(self.log.parent.glob('.*.tmp')), [])

    def test_notify(self):
        waiter = subprocess.Popen([sys.executable, '-c',
                                   'import signal,sys,time\n'
                                   'signal.signal(signal.SIGUSR1, lambda *a: sys.exit(7))\n'
                                   'print(flush=True); time.sleep(10)'], stdout=subprocess.PIPE)
        self.addCleanup(waiter.stdout.close)
        self.addCleanup(waiter.kill)
        waiter.stdout.readline()
        self.write([('ls', ())], [])
        stamp = Path(self.env['XDG_RUNTIME_DIR']) / 'fishsense' / f'{waiter.pid}.rank'
        self.run_rank('--notify', pid=waiter.pid)
        self.assertIsNone(waiter.poll())
        self.assertFalse(stamp.with_suffix('.result').exists())
        stamp.write_text('1\n')
        self.run_rank('--notify', pid=waiter.pid)
        self.assertEqual(waiter.wait(5), 7)
        self.assertEqual((Path(self.env['XDG_RUNTIME_DIR']) / 'fishsense' / f'{waiter.pid}.result')
                         .read_text(), 'ranked\t0\t\n')
        self.assertEqual([i.cmd for i in self.shadow(waiter.pid)], ['ls'])

    def test_stale_rank_does_not_overwrite(self):
        self.write([('ls', ())], [])
        stamp = Path(self.env['XDG_RUNTIME_DIR']) / 'fishsense' / f'{self.pid}.rank'
        stamp.parent.mkdir(parents=True)
        stamp.write_text('2\n')
        self.run_rank('--seq', '1')
        self.assertEqual(self.shadow(), [])
        self.run_rank('--seq', '2')
        self.assertEqual([i.cmd for i in self.shadow()], ['ls'])

    def test_rank_after_exit_writes_nothing(self):
        self.write([('ls', ())], [])
        self.run_rank('--seq', '1', '--notify')
        self.assertEqual(list(self.fish_data.glob('fishsense_*')), [])
        self.assertEqual(list((Path(self.env['XDG_RUNTIME_DIR']) / 'fishsense').glob('*')), [])

    def test_performance(self):
        for n, budget in [(1000, 0.150), (50000, 0.600)]:
            hist = [(f'cmd{i % 997} --flag {i} /some/path/{i}', (f'/some/path/{i}',) if i % 5 == 0 else ())
                    for i in range(n)]
            now = int(time.time())
            log = [common.LogRec(now - (n - i) * 60, 1000 + i // 50, int(i % 7 == 0),
                                 f'/home/u/p{i % 40}', hist[(i * 7919) % n][0])
                   for i in range(min(n, rank.LOG_MAX))]
            self.write(hist, log)
            best = min(self.run_rank(cwd='/home/u/p3', last=hist[-1][0]) for _ in range(3))
            self.assertEqual(len(self.shadow()), n)
            self.assertLess(best, budget, f'{n} items: {best * 1000:.0f} ms')


if __name__ == '__main__':
    unittest.main()
