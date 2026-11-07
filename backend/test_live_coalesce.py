import tempfile
import unittest
import uuid
from pathlib import Path
from live_room import LiveRoom
from game_director import GameDirector,GameStore
from test_game_director import Model,Adapter

class MergeTests(unittest.TestCase):
    def test_live_uid_order_dedup_and_other_viewer(self):
        live=LiveRoom(1,None)
        live.receive('same','first',1,uid=1); live.receive('same','other',2,uid=2)
        live.receive('renamed','second',3,uid=1); live.receive('renamed','second',3,uid=1)
        consumer=str(uuid.uuid4()); item=live.next(consumer)
        self.assertEqual([m['text'] for m in item['messages']],['first','second'])
        self.assertEqual(live.next(consumer)['text'],'other')
        self.assertIsNone(live.next(consumer))
    def test_unknown_uid_never_merges_and_expired_message_stays_out(self):
        live=LiveRoom(1,None); consumer=str(uuid.uuid4())
        live.receive('same','one',1); live.receive('same','two',2)
        self.assertEqual(live.next(consumer)['mergedCount'],1)
        live.next(consumer)
        live.receive('x','fresh',3,uid=3); live.receive('x','stale',4,uid=3)
        live.queue[-1]['received']-=100
        self.assertEqual(live.next(consumer)['text'],'fresh'); self.assertIsNone(live.next(consumer))
    def test_bounded_batch(self):
        live=LiveRoom(1,None)
        for i in range(8): live.receive('x',str(i),i,uid=1)
        c=str(uuid.uuid4()); self.assertEqual(live.next(c)['mergedCount'],5)
        self.assertEqual(live.next(c)['mergedCount'],3)
    def test_receiver_preserves_uid_at_game_boundary(self):
        got=[]; live=LiveRoom(1,None,event_sink=lambda *a,**kw:got.append((a,kw)) or True)
        live.receive('x','build',1,uid=123)
        self.assertEqual(got[0][1]['uid'],'123'); self.assertEqual(len(live.queue),0)
    def test_reply_jobs_merge_without_replaying_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            model=Model(); adapter=Adapter(); d=GameDirector(GameStore(Path(tmp)/'events.db'),model,adapter)
            calls=[]; model.bundle_reply=lambda jobs: calls.append(jobs) or 'combined'
            for i,uid in enumerate(('1','2','1')):
                d.queue_reply(dict(id=str(i),uid=uid,name='same',text=str(i),received=100), 'chat',(str(i),False,None),'chat')
            self.assertTrue(d.process_reply()); self.assertEqual(len(calls[0]),2)
            self.assertEqual(d.replies[0]['comment']['mergedCount'],2)
            self.assertEqual(adapter.writes,[])
            self.assertEqual(d.reply_jobs.unfinished_tasks,1)
            d.process_reply(); self.assertEqual(d.reply_jobs.unfinished_tasks,0)
    def test_commands_keep_separate_durable_ids_and_ready_replies_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=GameDirector(GameStore(Path(tmp)/'events.db'),Model(),Adapter())
            d.submit('x','first','a',uid=1); d.submit('x','second','b',uid=1)
            self.assertEqual(len(d.pending),2)
            self.assertIsNone(d.submit('x','first','a',uid=1))
            for item in d.pending: d.reply(item,'confirmed '+item['text'],'command',evidence=item['id'])
            r=d.next_reply(str(uuid.uuid4())); self.assertEqual(r['mergedCount'],2)
            self.assertEqual([x['evidence'] for x in r['replies']],['a','b'])
            self.assertEqual(d.adapter.writes,[])

if __name__=='__main__': unittest.main()
