import json
import tempfile
import unittest
from pathlib import Path
from game_director import GameStore, GameDirector
from game_learning import event_summary
from test_game_director import Model, Adapter


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=GameStore(Path(self.tmp.name)/'state.db')
    def tearDown(self): self.tmp.cleanup()
    def step(self, session='s', adopted=None, cash=1000):
        v=dict(sessionId=session,player=0,cash=cash,owned=[],entities=[])
        return self.store.record('step',dict(before=v,after=v,plan={'method':'observe','params':{}},
            receipt={},adoptedLessonIds=adopted or [],lessonTests=[dict(lessonId=i,field='method',expect='油田归己方') for i in (adopted or [])],expectedOutcome='油田归己方'))
    def test_context_retrieves_old_economy_not_only_latest_twelve(self):
        e=self.step(); old=self.store.learn('经济缺钱时，用工程师占领中立油田增加现金收入',[e])
        for i in range(20): self.store.learn(f'移动等待抵达，需要回读坐标编号{i}',[e])
        recalled=self.store.recall({'cash':500,'strategy':{'phase':'restore-economy'}})
        self.assertEqual(recalled[0]['id'],old)
        self.assertLessEqual(len(recalled),4)
    def test_whole_match_includes_early_steps_and_later_capture_not_other_match(self):
        first=self.step(); wrong=self.step('other')
        for _ in range(80): self.step()
        oil=self.store.record('oil-captured',{'session':'s','target':'oil','evidence':'owned-observation'})
        bundle=self.store.learning.bundle('s'); ids={e['id'] for e in bundle['timeline']}
        self.assertIn(first,ids); self.assertIn(oil,ids); self.assertNotIn(wrong,ids)
        self.assertEqual(bundle['totalSteps'],81); self.assertLess(bundle['sampledSteps'],81)
    def test_evaluation_requires_adoption_and_later_evidence_atomically(self):
        first=self.step(); lesson=self.store.learn('经济不足先占油田',[first]); adopted=self.step(adopted=[lesson])
        bundle=self.store.learning.bundle('s')
        proposal=dict(text='新经验',tags=['economy'],when='缺钱',expect='收入增加',evidence=[first])
        evaluation=dict(lessonId=lesson,stepId=adopted,verdict='supported',attribution='model-decision',testedField='method',decisionReason='选择观察后核对预期',evidence=[first],reason='应该成功')
        with self.assertRaises(ValueError): self.store.learning.apply({'lessons':[proposal],'evaluations':[evaluation]},bundle)
        with self.store.connect() as db: self.assertEqual(db.execute('select count(*) from lessons').fetchone()[0],1)
        later=self.store.record('oil-captured',{'session':'s','target':'oil'})
        bundle=self.store.learning.bundle('s'); evaluation['evidence']=[later]
        self.store.learning.apply({'evaluations':[evaluation]},bundle)
        self.store.learning.apply({'evaluations':[evaluation]},bundle)
        with self.store.connect() as db: self.assertEqual(db.execute('select count(*) from lesson_evaluations').fetchone()[0],1)
        self.assertEqual(self.store.recall()[0]['supported'],1)
    def test_inconclusive_stays_pending_and_revisions_keep_original(self):
        e=self.step(); old=self.store.learn('旧经济经验',[e]); use=self.step(adopted=[old]); b=self.store.learning.bundle('s')
        self.store.learning.apply({'evaluations':[dict(lessonId=old,stepId=use,verdict='inconclusive',evidence=[use],reason='尚未看到结果')]},b)
        self.assertNotIn(use,self.store.learning.bundle('s')['pendingApplications'])
        self.step()
        self.assertIn(use,self.store.learning.bundle('s')['pendingApplications'])
        new=self.store.learning.apply({'lessons':[dict(text='修正后的经济经验',evidence=[e],supersedes=[old],tags=['economy'])]},b)[0]
        self.assertEqual([x['id'] for x in self.store.recall()],[new])
        with self.store.connect() as db:
            self.assertEqual(db.execute('select text,status from lessons where id=?',(old,)).fetchone(),('旧经济经验','candidate'))
    def test_unknown_adoption_id_rejected_before_game_write(self):
        adapter=Adapter(); model=Model()
        model.plan=lambda *a:dict(method='observe',params={},lessonIds=['invented'])
        director=GameDirector(self.store,model,adapter);director.set_enabled(True);director.tick()
        self.assertEqual(adapter.writes,[])

    def test_learning_model_cannot_silently_omit_adoption_contract(self):
        adapter=Adapter(); model=Model(); model.reflect=lambda bundle:{}
        model.plan=lambda *a:dict(method='observe',params={})
        director=GameDirector(self.store,model,adapter);director.set_enabled(True);director.tick()
        self.assertEqual(adapter.writes,[])
        self.assertTrue(any('explicit lessonIds' in e['data'].get('error','') for e in self.store.events()))
    def test_adoption_recorded_separately_from_recall(self):
        e=self.step('s1'); lesson=self.store.learn('移动须等待',[e]); model=Model(); adapter=Adapter()
        model.plan=lambda *a:dict(method='observe',params={},lessonIds=[lesson],lessonTests=[dict(lessonId=lesson,field='method',expect='下一轮位置改变')],expectedOutcome='下一轮位置改变')
        director=GameDirector(self.store,model,adapter);director.set_enabled(True);director.tick()
        latest=[e for e in self.store.events() if e['kind']=='step'][-1]['data']
        self.assertEqual(latest['adoptedLessonIds'],[lesson]); self.assertIn(lesson,latest['recalledLessonIds'])
    def test_snapshot_retains_owned_oil_when_not_in_view(self):
        e=dict(id='x',kind='step',created=1,data={'after':{'player':0,'entities':[],
            'owned':[dict(id='oil',owner=0,type='CAOILD')]},'localSkill':True})
        result=event_summary(e)
        self.assertEqual(result['after']['ownedOil'],['oil']); self.assertEqual(result['origin'],'local-controller')

    def test_partial_evaluation_keeps_other_adoption_pending(self):
        e=self.step(); a=self.store.learn('经济条件',[e]); b=self.store.learn('建造条件',[e])
        use=self.step(adopted=[a,b]); later=self.step()
        bundle=self.store.learning.bundle('s')
        self.store.learning.apply({'evaluations':[dict(lessonId=a,stepId=use,verdict='supported',attribution='model-decision',testedField='method',decisionReason='选择观察后核对预期',evidence=[later],reason='后续结果支持')]},bundle)
        self.assertIn(use,self.store.learning.bundle('s')['pendingApplications'])

    def test_retirement_preserves_original_record(self):
        e=self.step(); old=self.store.learn('旧经验',[e]); bundle=self.store.learning.bundle('s')
        self.store.learning.apply({'retire':[dict(lessonId=old,evidence=[e],reason='条件已不适用')]},bundle)
        self.assertEqual(self.store.recall(),[])
        with self.store.connect() as db:
            self.assertEqual(db.execute('select text from lessons where id=?',(old,)).fetchone()[0],'旧经验')

    def test_local_placement_cannot_be_claimed_as_model_choice(self):
        e=self.step(); lesson=self.store.learn('选址经验',[e]); use=self.step(adopted=[lesson]); later=self.step()
        with self.store.connect() as db:
            raw=json.loads(db.execute('select data from events where id=?',(use,)).fetchone()[0])
            raw['plan']={'method':'build','params':{'type':'GAREFN','cells':[{'x':1,'y':2}]}}
            db.execute('update events set data=? where id=?',(json.dumps(raw),use))
        bundle=self.store.learning.bundle('s')
        evaluation=dict(lessonId=lesson,stepId=use,verdict='supported',evidence=[later],reason='落地',
            attribution='model-decision',testedField='params.cells',decisionReason='我会选址')
        self.store.learning.apply({'evaluations':[evaluation]},bundle)
        with self.store.connect() as db:
            audit=json.loads(db.execute('select data from lesson_evaluations').fetchone()[0])
            self.assertEqual(audit['attribution'],'unknown')
            self.assertEqual(audit['proposedAttribution'],'model-decision')
        evaluation['attribution']='tool-assisted'
        self.store.learning.apply({'evaluations':[evaluation]},bundle)
        self.assertEqual(self.store.recall()[0]['supported'],0)

    def test_legacy_credit_is_not_ranked_and_reaudit_keeps_original(self):
        e=self.step();lesson=self.store.learn('选址经验',[e]);use=self.step(adopted=[lesson]);later=self.step()
        old={'verdict':'supported','reason':'旧结论'}
        with self.store.connect() as db:
            db.execute('insert into lesson_evaluations values (?,?,?,?,?,?)',('old',lesson,use,'s',1,json.dumps(old)))
            db.execute('insert into lesson_details values (?,?)',(lesson,json.dumps({'supported':1})))
        self.assertEqual(self.store.recall()[0]['supported'],0)
        bundle=self.store.learning.bundle('s');self.assertIn(use,bundle['pendingApplications'])
        self.store.learning.apply({'evaluations':[dict(lessonId=lesson,stepId=use,verdict='supported',evidence=[later],reason='工具执行',attribution='tool-assisted')]},bundle)
        with self.store.connect() as db:
            self.assertEqual(json.loads(db.execute("select data from lesson_evaluations where id='old'").fetchone()[0]),old)
            self.assertEqual(db.execute('select count(*) from evaluation_audits').fetchone()[0],1)

if __name__=='__main__': unittest.main()
