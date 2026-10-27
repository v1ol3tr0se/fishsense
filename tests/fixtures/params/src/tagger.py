#!/usr/bin/env python3
"""Tag photos.

Usage: tagger [options] PHOTO
"""
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('-i', '--input', type=Path, required=True, help='folder with photos')
p.add_argument('--format', choices=['jpg', 'png', 'webp'], default='jpg', help='output format (default: %(default)s)')
p.add_argument('-v', '--verbose', action='store_true', help='talk more')
p.add_argument('--color', action=argparse.BooleanOptionalAction, help='colors')
p.add_argument('-n', '--count', type=int, metavar='N', help='how many')
sub = p.add_subparsers()
sub.add_parser('scan', help='scan folders')
sub.add_parser('export', help='export tags')
args = p.parse_args()
