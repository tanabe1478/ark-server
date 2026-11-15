"""Runs tests/asa_cases.py in a child process with ARK_GAME=asa (the profile is fixed at import)."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AsaProfileTests(unittest.TestCase):
    def test_asa_cases(self):
        done = subprocess.run([sys.executable, '-m', 'unittest', '-q', 'tests.asa_cases'], cwd=ROOT,
                              env=os.environ | {'ARK_GAME': 'asa'}, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr[-4000:])


if __name__ == '__main__':
    unittest.main()
