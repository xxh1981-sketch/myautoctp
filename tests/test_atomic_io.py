"""atomic_io unit tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atomic_io import atomic_write_text


class TestAtomicIo(unittest.TestCase):

    def test_atomic_write_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'nested', 'data.csv')
            atomic_write_text(path, 'a,b\n1,2\n')
            with open(path, encoding='utf-8') as f:
                self.assertEqual(f.read(), 'a,b\n1,2\n')

    def test_atomic_write_replaces_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'x.txt')
            atomic_write_text(path, 'old')
            atomic_write_text(path, 'new')
            with open(path, encoding='utf-8') as f:
                self.assertEqual(f.read(), 'new')


if __name__ == '__main__':
    unittest.main()
