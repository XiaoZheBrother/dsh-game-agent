import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from game_director import GameStore

class PackagedReviewTests(unittest.TestCase):
    def test_invalid_item_does_not_discard_valid_lesson(self):
        with tempfile.TemporaryDirectory() as root:
            store=GameStore(Path(root)/'state.db')
            event=store.record('step',dict(before={'sessionId':'s'},after={},plan={},receipt={}))
            bundle=store.learning.bundle('s')
            with store.connect() as db:
                result=store.learning.apply_partial({'lessons':[{'text':'candidate','evidence':[event]}, {'text':'bad','evidence':['invented']}]},bundle,db)
            self.assertEqual(result['accepted'],1)
            self.assertEqual(len(result['rejected']),1)
            with store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM lessons').fetchone()[0],1)

    def test_parallel_connections_preserve_all_events(self):
        with tempfile.TemporaryDirectory() as root:
            stores=[GameStore(Path(root)/'state.db') for _ in range(2)]
            with ThreadPoolExecutor(max_workers=4) as pool:
                ids=list(pool.map(lambda i:stores[i%2].record('observed',{'n':i}),range(80)))
            with stores[0].connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],len(ids))
