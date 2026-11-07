import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from game_director import GameStore
from lesson_lifecycle import LessonLifecycle


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.s=GameStore(Path(self.tmp.name)/'state.db')
        e=self.s.record('step',{})
        self.lesson=self.s.learn('人工经验：先发展经济',[e])
        with self.s.connect() as db:
            db.execute('INSERT INTO lesson_details VALUES (?,?)',(self.lesson,json.dumps({'origin':'assistant-teaching','tags':['economy']})))
        self.life=LessonLifecycle(self.s)
    def tearDown(self): self.tmp.cleanup()
    def counterexample(self,session='a',kind='A',code=0,uncertain=False):
        step=self.s.record('step',dict(before={'sessionId':session},after={},
            plan={'method':'act','params':{'action':'produce','type':kind}},
            receipt={'exitCode':code,'uncertain':uncertain},adoptedLessonIds=[self.lesson],
            lessonTests=[{'lessonId':self.lesson,'field':'params.type','expect':'收入增加'}]))
        after=self.s.record('step',{'before':{'sessionId':session},'after':{}})
        v={'stepId':step,'lessonId':self.lesson,'policyVersion':2,'verdict':'contradicted',
           'attribution':'model-decision','testedField':'params.type','evidence':[after],'reason':'未达到预期'}
        with self.s.connect() as db:
            db.execute('INSERT INTO lesson_evaluations VALUES (?,?,?,?,?,?)',(uuid.uuid4().hex,self.lesson,step,session,time.time(),json.dumps(v)))
        return after
    def threshold(self):
        self.counterexample('a','A');self.counterexample('a','B');return self.counterexample('b','A')
    def model(self,action='retire'):
        class Model:
            def review_lesson(_,bundle):
                return dict(action=action,reason='对照跨局结果缩小条件',evidence=[e['id'] for e in bundle['evidence']],
                            text='只在收入不足时发展经济',when='收入不足',expect='经济恢复')
        return Model()
    def test_dispute_then_suspend_across_games(self):
        self.counterexample();self.counterexample();self.counterexample()
        self.assertEqual(len(self.life.signals()[self.lesson]['cases']),1)
        self.assertEqual(self.s.recall()[0]['reviewStatus'],'disputed')
        self.counterexample('a','B');self.assertEqual(self.life.signals()[self.lesson]['state'],'disputed')
        self.counterexample('b','A');self.assertEqual(self.s.recall(),[])
    def test_rejected_and_uncertain_do_not_count(self):
        self.counterexample(code=1);self.counterexample(uncertain=True)
        self.assertEqual(self.life.signals(),{})
    def test_retirement_preserves_original(self):
        self.threshold();self.life.run_one(self.model())
        self.assertEqual(self.s.recall(),[])
        with self.s.connect() as db:
            self.assertEqual(db.execute('SELECT text FROM lessons WHERE id=?',(self.lesson,)).fetchone()[0],'人工经验：先发展经济')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lesson_evaluations').fetchone()[0],3)
        self.assertFalse(self.life.run_one(self.model()))
    def test_revision_replaces_recall_and_preserves_source(self):
        self.threshold();self.life.run_one(self.model('revise'))
        recalled=self.s.recall();self.assertEqual(len(recalled),1)
        self.assertEqual(recalled[0]['revises'],self.lesson)
        self.assertEqual(recalled[0]['origin'],'model-special-review')
    def test_retain_requires_new_evidence_to_requeue(self):
        self.threshold();self.life.run_one(self.model('retain'))
        self.assertEqual(len(self.s.recall()),1)
        self.assertFalse(self.life.run_one(self.model()))
        self.counterexample('c','A');self.assertEqual(self.s.recall()[0]['reviewStatus'],'disputed')
    def test_bad_review_retries_without_mutating_lesson(self):
        self.threshold()
        class Bad:
            def review_lesson(self,b):return {'action':'retire','reason':'坏引用','evidence':['invented']}
        self.life.run_one(Bad())
        with self.s.connect() as db:
            state,attempts=db.execute('SELECT state,attempts FROM lesson_reviews').fetchone()
            self.assertEqual((state,attempts),('pending',1))
            self.assertNotIn('retired',json.loads(db.execute('SELECT data FROM lesson_details WHERE id=?',(self.lesson,)).fetchone()[0]))
        self.assertEqual(self.s.recall(),[])

if __name__=='__main__':unittest.main()
