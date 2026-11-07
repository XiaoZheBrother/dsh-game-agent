import json
import tempfile
import threading
import unittest
from pathlib import Path
from game_director import GameStore
from review_jobs import ReviewJobs


class Model:
    def __init__(self): self.calls=[]; self.responses=[{}]
    def reflect(self,bundle):
        self.calls.append(bundle)
        response=self.responses.pop(0)
        if isinstance(response,Exception): raise response
        return response


class ReviewJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=GameStore(Path(self.tmp.name)/'db')
        self.step=self.store.record('step',dict(before={'sessionId':'s'},after={},plan={},receipt={}))
        self.now=1000;self.model=Model();self.jobs=ReviewJobs(self.store,self.model,lambda:self.now)
    def tearDown(self):self.tmp.cleanup()
    def test_invalid_output_persists_and_repairs_after_restart(self):
        self.model.responses=[{'lessons':[{}]*6}]
        self.jobs.enqueue('s');self.jobs.run_one()
        self.assertEqual(self.jobs.status()[0]['state'],'pending')
        self.assertFalse(self.jobs.run_one())
        self.now+=15; model=Model(); restarted=ReviewJobs(self.store,model,lambda:self.now)
        self.assertTrue(restarted.run_one())
        self.assertIn('Unbounded reflection',model.calls[0]['repairRequest']['error'])
        self.assertEqual(restarted.status()[0]['state'],'done')
    def test_failures_are_bounded_and_visible(self):
        self.model.responses=[ConnectionError('offline')]*5
        self.jobs.enqueue('s')
        for _ in range(5): self.jobs.run_one();self.now+=1000
        self.assertEqual(self.jobs.status()[0]['state'],'needs-attention')
        self.assertFalse(self.jobs.run_one())
    def test_lease_excludes_second_worker_and_preserves_new_request(self):
        started=threading.Event();release=threading.Event()
        def reflect(b):started.set();release.wait(5);return {}
        self.model.reflect=reflect;self.jobs.enqueue('s')
        thread=threading.Thread(target=self.jobs.run_one);thread.start();self.assertTrue(started.wait(3))
        second=ReviewJobs(self.store,Model(),lambda:self.now)
        self.assertFalse(second.run_one())
        self.now+=1;second.enqueue('s');release.set();thread.join(5)
        self.assertEqual(second.status()[0]['state'],'pending')
    def test_expired_claim_is_recovered(self):
        self.jobs.enqueue('s')
        with self.store.connect() as db:
            db.execute("UPDATE reflection_jobs SET state='running',lease='dead',leaseUntil=999")
        self.jobs.run_one();self.assertEqual(self.jobs.status()[0]['state'],'done')
    def test_result_and_job_commit_are_atomic(self):
        original=self.store.learning.apply
        def fail(result,bundle,db=None):
            original(result,bundle,db=db)
            raise RuntimeError('simulated failure after insert')
        self.store.learning.apply=fail;self.jobs.enqueue('s');self.jobs.run_one()
        with self.store.connect() as db:
            self.assertEqual(db.execute("select count(*) from events where kind='learning-reflection'").fetchone()[0],0)
        self.assertEqual(self.jobs.status()[0]['state'],'pending')

    def test_old_unknowns_do_not_starve_remaining_batches(self):
        lesson=self.store.learn('经验',[self.step])
        for _ in range(8):
            self.store.record('step',dict(before={'sessionId':'s'},after={},plan={},adoptedLessonIds=[lesson]))
        def reflect(bundle):
            return {'evaluations':[dict(**pair,verdict='inconclusive',evidence=[pair['stepId']],reason='没有结果证据') for pair in bundle['pendingTests']]}
        self.model.reflect=reflect;self.jobs.enqueue('s');self.jobs.run_one()
        self.assertEqual(self.jobs.status()[0]['state'],'pending')
        self.now+=30;self.jobs.run_one();self.assertEqual(self.jobs.status()[0]['state'],'done')
        with self.store.connect() as db:
            self.assertEqual(db.execute('select count(*) from evaluation_deferrals').fetchone()[0],8)

if __name__=='__main__':unittest.main()
