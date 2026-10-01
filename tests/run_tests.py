"""Discover offline unittest modules without importing GPU integration scripts."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['PYTHONUTF8'] = '1'


def main():
    names = []
    for path in sorted((ROOT/'tests').glob('*_test.py')):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        if any(isinstance(node,ast.ClassDef) and any(
            isinstance(base,ast.Attribute) and base.attr=='TestCase' or
            isinstance(base,ast.Name) and base.id=='TestCase' for base in node.bases)
            for node in tree.body):
            names.append('tests.'+path.stem)
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    subprocess.run([sys.executable,'-X','utf8',str(ROOT/'tests/asr_lyrics_test.py')],cwd=ROOT,check=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
