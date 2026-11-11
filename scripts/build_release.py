"""Build a local wheel only after checking pinned artwork; never calls AWS."""
import argparse
import hashlib
from pathlib import Path
import runpy
import subprocess
import sys

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.output_dir.exists():
        parser.error('Use a new output directory; existing artifacts are preserved')
    definitions = runpy.run_path(str(root / 'scripts/download_aws_icons.py'))
    for name, expected in definitions['EXPECTED_SHA256'].items():
        path = definitions['DEST'] / f'{name}.png'
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            parser.error(f'Missing/unverified icon {name}; run scripts/download_aws_icons.py')
    if not (root / 'LICENSE').is_file() or not (definitions['DEST'] / 'NOTICE.txt').is_file():
        parser.error('Project LICENSE and icon NOTICE are required')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-cache-dir', '--no-index', '--no-deps',
                    '--no-build-isolation', str(root), '--wheel-dir', str(args.output_dir.resolve())], check=True)

if __name__ == '__main__':
    main()
