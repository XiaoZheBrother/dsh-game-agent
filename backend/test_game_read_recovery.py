import subprocess
import unittest
from game_adapter import Ra2Adapter


class ReadRecoveryTests(unittest.TestCase):
    def test_transport_timeout_is_normalized_only_for_reads(self):
        adapter = Ra2Adapter.__new__(Ra2Adapter)
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(['node', 'control'], 40)
        adapter.call = timeout
        for method, expected in ((adapter.observe, '无法读取当前战况'),
                                 (adapter.native_status, '无法确认原生对局状态')):
            with self.assertRaisesRegex(RuntimeError, expected): method()
        with self.assertRaises(subprocess.TimeoutExpired):
            adapter.next_battle({'pageId': '1'}, 'session', 'command')


if __name__ == '__main__': unittest.main()
