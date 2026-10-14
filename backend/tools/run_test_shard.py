"""Run a deterministic portion of the complete collected backend suite."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=int, required=True)
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--output', type=Path, default=Path('logs'))
    parser.add_argument('--list-only', action='store_true')
    args = parser.parse_args()
    if args.count < 1 or not 0 <= args.index < args.count:
        parser.error('index must be in [0, count), count must be positive')
    backend = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    collection = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q',
        '-p', 'no:cacheprovider'], cwd=backend, capture_output=True, text=True, encoding='utf-8')
    (output/'collection.log').write_text(collection.stdout+collection.stderr, encoding='utf-8')
    if collection.returncode:
        print(collection.stdout, collection.stderr)
        return collection.returncode
    nodes = [line for line in collection.stdout.splitlines() if line.startswith('tests/') and '::' in line]
    selected = nodes[args.index::args.count]
    if not selected:
        parser.error('empty shard; inspect collection.log')
    nodefile = output/'nodes.args'
    nodefile.write_text('\n'.join(selected), encoding='utf-8')
    (output/'coverage.json').write_text(json.dumps({'collected':len(nodes),
        'shard':args.index, 'shards':args.count, 'selected':len(selected)}, indent=2), encoding='utf-8')
    if args.list_only:
        return 0
    # conftest retains Python stacks after 120s, without native timer unwinding.
    return subprocess.call([sys.executable, '-m', 'pytest', '@'+str(nodefile), '-q',
        '-p', 'no:cacheprovider', '-o', 'faulthandler_timeout=0',
        '--junitxml='+str(output/'pytest.xml'), '--basetemp='+str(output/'pytest-tmp')], cwd=backend)


if __name__ == '__main__':
    raise SystemExit(main())
