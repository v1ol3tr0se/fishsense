#!/usr/bin/env python3
"""fishsense helper: `fishsense.py <subcommand> [args]`. Called by the fish side, never by hand."""
import importlib
import os
import signal
import sys

for _sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(_sig, lambda n, _: sys.exit(128 + n))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SUBS = {
    'rank': 'rank',
    'next': 'params',
    'compgen': 'params',
    'tldr-update': 'params',
    'correct': 'correct',
    'ai': 'ai',
}


def doctor():
    import common
    print(f'python: {sys.version.split()[0]}')
    print(f'data: {common.data_dir()}')
    print(f'cache: {common.cache_dir()}')
    for mod in ('rank', 'params', 'correct', 'ai'):
        try:
            lines = getattr(importlib.import_module(mod), 'doctor_lines', lambda: [])()
        except Exception as e:
            lines = [f'{mod}: error: {e}']
        for line in lines:
            print(line)
    return 0


def main(argv):
    extra = os.environ.pop('FISHSENSE_ARGS', '')
    argv = argv + (extra.split('\x1f') if extra else [])
    if not argv:
        return 2
    sub, rest = argv[0], argv[1:]
    try:
        if sub == 'doctor':
            return doctor()
        if sub == 'gc':
            import common
            common.gc_shadows(rest[rest.index('--fish-data') + 1])
            return 0
        if sub not in SUBS:
            return 2
        return importlib.import_module(SUBS[sub]).main(sub, rest) or 0
    except Exception:
        if os.environ.get('FISHSENSE_DEBUG'):
            raise
        return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
