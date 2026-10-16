import hashlib
import pathlib
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from modelhub_kit.core import GuardError, run


class FiniteInputTransport(unittest.TestCase):
    def test_finite_input_closes_eof_while_output_is_drained(self):
        payload = b'application-source\x00' * 65536
        child = ('import hashlib,sys; '
                 'sys.stdout.write("x"*100000); sys.stdout.flush(); '
                 'data=sys.stdin.buffer.read(); '
                 'print(hashlib.sha256(data).hexdigest())')
        result = run([sys.executable, '-B', '-c', child], input_data=payload, timeout=5)
        self.assertEqual(result['exit'], 0)
        self.assertFalse(result['timedOut'])
        self.assertFalse(result['inputFailed'])
        self.assertEqual(result['inputSent'], len(payload))
        self.assertTrue(result['outputTruncated'])
        self.assertTrue(result['output'].strip().endswith(hashlib.sha256(payload).hexdigest()))

    def test_nonreading_child_is_bounded_with_pending_input(self):
        started = time.monotonic()
        result = run([sys.executable, '-B', '-c', 'import time; time.sleep(60)'],
                     input_data=b'x' * 1024**2, timeout=0.15)
        self.assertTrue(result['timedOut'])
        self.assertTrue(result['inputFailed'])
        self.assertLess(time.monotonic() - started, 4)

    def test_early_closed_pipe_reports_incomplete_delivery(self):
        result = run([sys.executable, '-B', '-c', 'import os; os.close(0); print("closed")'],
                     input_data=b'x' * 1024**2, timeout=5)
        self.assertEqual(result['exit'], 0)
        self.assertTrue(result['inputFailed'])
        self.assertIn('closed', result['output'])

    def test_text_is_not_implicitly_encoded_or_executed(self):
        with self.assertRaises(GuardError):
            run([sys.executable], input_data='not-bytes')


if __name__ == '__main__':
    unittest.main()
