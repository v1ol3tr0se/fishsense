import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / 'conf.d' / 'fishsense'
sys.path.insert(0, str(HERE))
import common
import correct

KNOWN = {'git', 'gtf', 'ls', 'sh', 'su', 'rm', 'mv', 'vi', 'vim', 'sudo', 'env', 'pacman', 'man', 'cpan',
         'cat', 'less', 'make'}
GIT_SUBS = {'status', 'stash', 'stage', 'reset', 'rebase', 'restore', 'revert', 'branch', 'push', 'log'}
USES = {'git': 50, 'ls': 40, 'sh': 2, 'rm': 30, 'mv': 9, 'vi': 1, 'vim': 3, 'pacman': 5, 'status': 9}


def run(line, cwd='/nonexistent', uses=USES, known=KNOWN, subs=None, funcs=frozenset(), aliases={}):
    return correct.correct_line(
        line, cwd, lambda w: w in known or w in funcs, lambda: known | funcs,
        {'git': GIT_SUBS} if subs is None else subs,
        lambda names, cmd: {n: uses.get(n, 0) for n in names}, lambda w: False, aliases)


class Correct(unittest.TestCase):
    def test_command_word(self):
        self.assertEqual(run('gti status'), ('fix', 'git status'))
        self.assertEqual(run('sl'), ('fix', 'ls'))
        self.assertEqual(run('sudo pcman -S x'), ('ask', 'sudo pacman -S x'))
        self.assertEqual(run('sduo pcman -S x'), ('ask', 'sudo pacman -S x'))

    def test_destructive_asks(self):
        self.assertEqual(run('mr file'), ('ask', 'rm file'))
        self.assertEqual(run('vm a b', uses={'mv': 9, 'vim': 3, 'rm': 1}), ('ask', 'mv a b'))
        self.assertEqual(run('git rset --hard'), ('ask', 'git reset --hard'))
        self.assertEqual(run('gti brnch -D x'), ('ask', 'git branch -D x'))
        for flags in ['--delete --force', '-d -f', '-df', '-f --delete']:
            self.assertEqual(run(f'gti branch {flags} x'), ('ask', f'git branch {flags} x'), flags)
        self.assertEqual(run('gti branch -d x'), ('fix', 'git branch -d x'))
        self.assertEqual(run('gti status && rm -rf x'), ('ask', 'git status && rm -rf x'))
        for tail in ['/usr/bin/rm victim', '\\rm victim', "'r'm victim", '/usr/bin/sudo ls', '/bin/mkfs.ext4 x']:
            self.assertEqual(run(f'gti status && {tail}'), ('ask', f'git status && {tail}'), tail)
        self.assertEqual(run('gti status; /usr/bin/git push'), ('ask', 'git status; /usr/bin/git push'))

    def test_fix_to_a_function_does_not_ask(self):
        self.assertEqual(run('clad', funcs={'clawd'}), ('fix', 'clawd'))
        self.assertEqual(run('unbka x.bak', funcs={'unbak'}), ('fix', 'unbak x.bak'))
        self.assertEqual(run('eshred x', funcs={'shred'}), ('ask', 'shred x'))

    def test_fix_to_an_alias_subcommand_asks(self):
        subs = {'git': GIT_SUBS | {'wipe', 'fl'}}
        aliases = {'git': {'wipe', 'fl'}}
        self.assertEqual(run('git wpie', subs=subs, aliases=aliases), ('ask', 'git wipe'))
        self.assertEqual(run('git lf', subs=subs, aliases=aliases), ('ask', 'git fl'))
        self.assertEqual(run('git stauts', subs=subs, aliases=aliases), ('fix', 'git status'))

    def test_local_node_binary_is_no_typo(self):
        subs = {'yarn': {'dev', 'lint', 'test', 'build', 'start', 'serve', 'format', 'preview', 'add', 'run'}}
        known = KNOWN | {'yarn'}
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'node_modules' / '.bin').mkdir(parents=True)
            for b in ('jest', 'eslint', 'vitest', 'nest'):
                (Path(d) / 'node_modules' / '.bin' / b).touch()
            (Path(d) / 'packages' / 'web').mkdir(parents=True)
            for cwd in (d, str(Path(d) / 'packages' / 'web')):
                for line in ('yarn jest --watch', 'yarn eslint .', 'yarn vitest', 'yarn nest start'):
                    self.assertIsNone(run(line, cwd=cwd, subs=subs, known=known), (cwd, line))
            self.assertEqual(run('yarn tset', cwd=d, subs=subs, known=known), ('fix', 'yarn test'))

    def test_ambiguous(self):
        self.assertIsNone(run('mr file', uses={'rm': 20, 'mv': 9}))
        self.assertIsNone(run('sl', uses={'ls': 3, 'sh': 2}))
        self.assertIsNone(run('gti status', uses={}))

    def test_subcommand(self):
        self.assertEqual(run('git stauts'), ('fix', 'git status'))
        self.assertEqual(run('gti stauts -s'), ('fix', 'git status -s'))
        self.assertIsNone(run('git stauts', subs={}))
        self.assertIsNone(run('git -C x stauts'))
        self.assertIsNone(run('npm x cowsay', subs={'npm': {'i', 'exec', 'install'}}, known=KNOWN | {'npm'}))
        self.assertIsNone(run('ip addr', subs={'ip': {'address', 'maddress', 'maddr', 'route'}},
                              known=KNOWN | {'ip'}))
        self.assertEqual(run('ip roue', subs={'ip': {'address', 'route'}}, known=KNOWN | {'ip'}),
                         ('fix', 'ip route'))
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'stauts').touch()
            self.assertIsNone(run('git stauts', cwd=d))

    def test_local_script_is_not_a_typo(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'run.sh').touch()
            known = KNOWN | {'rungs'}
            self.assertIsNone(run('run.sh', cwd=d, known=known))
            self.assertEqual(run('run.sh', known=known), ('fix', 'rungs'))

    def test_nothing_to_do(self):
        for line in ['git status', 'ls -la', 'cat fiel.txt', 'git add fiel.txt', './scirpt.sh',
                     '/usr/bin/gti', '~/bin/gti', ' gti status', 'FOO=1 gti status', '$EDITOR x',
                     '(echo gti) status', 'gti$x', 'g*i', 'g{t,i}', "'gti", 'g\\ti status', 'command -v gti',
                     'x', '42', 'git st$x', '', 'git sta.tus']:
            self.assertIsNone(run(line), line)

    def test_prefixes_and_bytes(self):
        self.assertEqual(run('env FOO=1 gti log'), ('fix', 'env FOO=1 git log'))
        self.assertEqual(run('time gti log'), ('fix', 'time git log'))
        self.assertEqual(run('sudo -u alex -E gti log'), ('ask', 'sudo -u alex -E git log'))
        self.assertEqual(run("'gti'  status   'a  b' | less # c"), ('fix', "git  status   'a  b' | less # c"))
        self.assertEqual(run('gti log>out; echo "$x"'), ('fix', 'git log>out; echo "$x"'))

    def test_distance(self):
        self.assertEqual(correct._dl('gti', 'git'), 1)
        self.assertEqual(correct._dl('stauts', 'status'), 1)
        self.assertEqual(correct._dl('kitten', 'sitting'), 3)
        self.assertIsNone(correct._best('pacmna', {'pacmanxx'}, lambda n: {}))
        self.assertEqual(correct._best('pacmna', {'pacman'}, lambda n: {}), 'pacman')

    def test_more_typos_on_longer_words(self):
        self.assertEqual(run('git pshu'), ('ask', 'git push'))
        self.assertEqual(run('git rsetroee'), ('ask', 'git restore'))
        self.assertEqual(run('fsatfecht', known=KNOWN | {'fastfetch'}, uses={'fastfetch': 4}), ('fix', 'fastfetch'))
        self.assertEqual(run('sytmeclt stop x', known=KNOWN | {'systemctl'}, uses={'systemctl': 1}),
                         ('ask', 'systemctl stop x'))

    def test_far_fix_only_to_something_used(self):
        known = KNOWN | {'permute', 'fastfetch'}
        self.assertIsNone(run('fsatfecht', known=known))
        self.assertIsNone(run('neomutt', known=known))
        self.assertEqual(run('neomutt', known=known, uses={'permute': 1}), ('fix', 'permute'))
        self.assertEqual(run('fastfecht', known=known, uses={}), ('fix', 'fastfetch'))


class Cli(unittest.TestCase):
    """Through the dispatcher, with a fake PATH and real history/log files."""

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bin_ = d / 'bin'
            bin_.mkdir()
            for name in ('git', 'gtf', 'ls'):
                (bin_ / name).write_text('#!/bin/sh\n')
                (bin_ / name).chmod(0o755)
            (d / 'fish').mkdir()
            (d / 'fish' / 'fish_history').write_text(common.format_fish_history(
                [('git log', ()), ('git status', ())], when=1))
            env = {'PATH': str(bin_), 'XDG_DATA_HOME': str(d / 'data'), 'XDG_CACHE_HOME': str(d / 'cache'),
                   'XDG_RUNTIME_DIR': str(d / 'run'), 'HOME': str(d)}
            common.write_log([common.LogRec(1, 1, 0, '/', 'git status')], d / 'data' / 'fishsense' / 'log.tsv')

            def cli(line, stdin):
                return subprocess.run(
                    [sys.executable, '-B', str(HERE / 'fishsense.py'), 'correct', '--cwd', common.enc(str(d)),
                     '--line', common.enc(line), '--fish-data', str(d / 'fish'), '--real-name', 'fish'],
                    input=stdin, capture_output=True, text=True, env=env, timeout=10)

            stdin = '#cmds\ncd\nabbr\n#sub git\nstatus\tShow the working tree status\nstash\tStash\n'
            r = cli('gti stauts', stdin)
            self.assertEqual((r.stdout, r.stderr), (f'fix\t{common.enc("git status")}\n', ''))
            self.assertEqual(cli('cd /tmp', stdin).stdout, '')
            self.assertEqual(cli('ls', '').stdout, '')
            (bin_ / 'nvim').write_text('#!/bin/sh\n')
            (bin_ / 'nvim').chmod(0o755)
            self.assertEqual(cli('nvm isntall lts', '#sub nvm\ninstall\nuse\n').stdout,
                             f'fix\t{common.enc("nvm install lts")}\n')
            self.assertEqual(cli('nvm instal lts', '#sub nvm\ninstall\nuse\n').stdout, '')
            self.assertEqual(cli('nvx instal', '#cmds\ncd\n#funcs\nnvm\n').stdout,
                             f'fix\t{common.enc("nvm instal")}\n')
            self.assertEqual(cli('gtf', '#cmds\ncd\n#funcs\ncd\ngit\n').stdout, '')
            stdin = '#sub git\nstatus\tShow the working tree status\nwipe\talias: reset --hard…\nwipe\n'
            self.assertEqual(cli('git wpie', stdin).stdout, f'ask\t{common.enc("git wipe")}\n')
            r = subprocess.run([sys.executable, '-B', str(HERE / 'fishsense.py'), 'correct'],
                               input=stdin, capture_output=True, text=True, timeout=10, env=dict(env, FISHSENSE_ARGS='\x1f'.join(
                                   ['--cwd', common.enc(str(d)), '--line', common.enc('git stauts'),
                                    '--fish-data', str(d / 'fish'), '--real-name', 'fish'])))
            self.assertEqual((r.stdout, r.stderr), (f'fix\t{common.enc("git status")}\n', ''))


if __name__ == '__main__':
    unittest.main()
