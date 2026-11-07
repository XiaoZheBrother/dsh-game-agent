import copy
import tempfile
import unittest
from pathlib import Path
from game_context import DecisionGate, compact, rows, step_summary
from game_director import GameDirector, GameStore
from test_game_director import Model, Adapter
from live_ledger import LiveLedger


class EfficiencyTests(unittest.TestCase):
    def test_gate_ignores_frames_and_movement_but_wakes_on_damage(self):
        now=[100]
        gate=DecisionGate(lambda: now[0])
        view={'sessionId':'s','player':0,'entities':[{'id':'u','owner':0,'health':100,'cell':{'x':1,'y':2}}]}
        gate.mark(copy.deepcopy(view))
        view['frame']=123;view['entities'][0]['cell']['x']=2
        self.assertIsNone(gate.reason(view))
        view['entities'][0]['health']=99
        self.assertEqual(gate.reason(view),'damage')
        gate.mark(copy.deepcopy(view));now[0]+=16
        self.assertEqual(gate.reason(view),'periodic-check')

    def test_compaction_preserves_ids_ownership_and_foundation(self):
        view={'entities':[{'id':'1','owner':2,'health':None,'cell':{'x':1,'y':2},'foundation':{'width':4,'height':3},'screen':{'x':300}}]}
        result=rows(compact(view)['entities'])[0]
        self.assertEqual(result,{k:v for k,v in view['entities'][0].items() if k!='screen'})
        self.assertIn('screen',view['entities'][0])

    def test_waits_use_no_model_and_viewer_bypasses_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            model=Model(); adapter=Adapter();calls=[]
            model.on_plan=lambda:calls.append(1)
            director=GameDirector(GameStore(Path(tmp)/'db'),model,adapter)
            director.set_enabled(True)
            for _ in range(10):director.tick()
            self.assertEqual(len(calls),1)
            director.submit('viewer','移动到右边');director.tick()
            self.assertEqual(len(calls),2)

    def test_build_waits_then_places_once_without_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            model=Model();adapter=Adapter();calls=[]
            view={'sessionId':'s1','player':0,'entities':[{'id':'base','owner':0,'kind':'building',
                  'cell':{'x':20,'y':20},'foundation':{'width':4,'height':4}}],
                  'sidebar':[{'type':'P','foundation':{'width':2,'height':2}}], 'production':[]}
            adapter.observe=lambda:copy.deepcopy(view)
            def execute(method, params, session):
                adapter.writes.append(method)
                if method=='act':view['production']=[{'type':'P','progress':1,'total':10}]
                if method=='place-building':view['production']=[]
                return {'uncertain':False,'exitCode':0}
            adapter.execute=execute
            def plan(*args):
                calls.append(1)
                return {'method':'build','params':{'type':'P','cells':[{'x':1,'y':2}]}}
            model.plan=plan
            director=GameDirector(GameStore(Path(tmp)/'db'),model,adapter);director.set_enabled(True)
            director.tick();director.tick();director.tick()
            self.assertEqual(adapter.writes,['act']);self.assertEqual(len(calls),1)
            view['production'][0]['progress']=10
            director.tick();director.tick()
            self.assertEqual(adapter.writes,['act','place-building']);self.assertEqual(len(calls),1)
            self.assertIsNone(director.skill)

    def test_viewer_and_pause_cancel_pending_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            director=GameDirector(GameStore(Path(tmp)/'db'),Model(),Adapter());director.set_enabled(True)
            director.skill={'session':'s1','type':'P','cells':[], 'deadline':1e20}
            director.submit('viewer','移动');director.tick()
            self.assertIsNone(director.skill)

    def test_new_round_never_receives_old_round_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=GameStore(Path(tmp)/'db');model=Model();adapter=Adapter()
            store.record('step', {'before':{'sessionId':'old'},'plan':{'method':'look'},'receipt':{'secretOldTarget':1}})
            director=GameDirector(store,model,adapter);director.set_enabled(True);director.tick()
            self.assertEqual(model.recent,[])
            director.skill={'type':'P'};director.set_enabled(False)
            self.assertIsNone(director.skill)

    def test_ledger_categories_reconcile_and_keep_old_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=LiveLedger(Path(tmp)/'ledger','test')
            usage={'total_tokens':30,'prompt_tokens':20,'completion_tokens':10}
            ledger.usage('1',usage);ledger.usage('2',usage,purpose='decision');ledger.usage('2',usage,purpose='decision')
            result=ledger.snapshot()
            self.assertEqual(result['consumed'],60)
            self.assertEqual(sum(c['tokens'] for c in result['usageCategories'].values()),60)
            self.assertEqual(result['usageCategories']['decision']['calls'],1)

    def test_review_preserves_health_before_after_and_no_embedded_views(self):
        event={'id':'e','data':{'before':{'entities':[{'id':'u','health':100}]},'after':{'entities':[{'id':'u','health':10}]},'receipt':{'events':[{'error':'failed','observation':{'huge':1}}]}}}
        summary=step_summary(event)
        self.assertEqual(rows(summary['previousChangedEntities'])[0]['health'],100)
        self.assertEqual(summary['receipt']['events'],[{'error':'failed'}])


if __name__=='__main__':unittest.main()
