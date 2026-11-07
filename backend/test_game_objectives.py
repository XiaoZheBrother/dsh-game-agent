import tempfile
import unittest
from pathlib import Path
from game_director import GameStore
from game_objectives import Objectives, validate_command_visibility


class ObjectiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.s=GameStore(Path(self.tmp.name)/'state.db');self.o=Objectives(self.s)
    def tearDown(self):self.tmp.cleanup()
    def test_goal_survives_restart_but_not_next_match(self):
        proposal=dict(action='set',objective='确认经济能否恢复',success='矿车恢复工作',reason='缺收入',topics=['economy'])
        self.o.validate(proposal,None,[]);self.o.accept(proposal,None,'a','step')
        loaded=Objectives(GameStore(self.s.path)).current('a')
        self.assertEqual(loaded['objective'],proposal['objective']);self.assertIsNone(self.o.current('b'))
        self.o.validate({'action':'continue'},loaded,[])
        with self.assertRaises(ValueError):self.o.validate(proposal,loaded,[])
        with self.assertRaises(ValueError):self.o.validate({'action':'complete','reason':'好了','evidenceIds':['fake']},loaded,[])
    def test_goal_completion_keeps_prior_history(self):
        p=dict(action='set',objective='侦察',success='获得敌情',reason='未知',topics=['scout'])
        self.o.accept(p,None,'a','one');old=self.o.current('a')
        done=dict(action='complete',reason='观察到目标',evidenceIds=['two'])
        self.o.validate(done,old,['two']);self.o.accept(done,old,'a','three')
        self.assertEqual(self.o.current('a')['status'],'completed-by-model')
        self.assertEqual(len([e for e in self.s.events() if e['kind']=='model-objective']),2)
    def test_round_trip_detected_without_inventing_scout_success(self):
        def view(x):return dict(sessionId='a',cash=500,owned=[dict(id='tank',cell={'x':x,'y':0})],strategy={'enemies':[]})
        for x in [0,10,20,10]:
            self.s.record('step',dict(before=view(x),plan={'method':'act','params':{'action':'move','unit':'tank','cell':{'x':x,'y':0}}},receipt={'exitCode':0}))
        result=self.o.context(view(0))['progress']
        self.assertEqual(result['loopingUnitIds'],['tank']);self.assertEqual(result['newEnemySightings'],0)
        self.assertEqual(result['medianNetDisplacement'],0)
    def test_partial_failure_is_visible_and_session_isolated(self):
        self.s.record('step',dict(before={'sessionId':'other'},plan={},receipt={'exitCode':1}))
        receipt={'exitCode':1,'events':[{'rejected':[{'error':'outside map'}]}]}
        self.s.record('step',dict(before={'sessionId':'a'},plan={},receipt=receipt))
        p=self.o.context({'sessionId':'a'})['progress']
        self.assertEqual(p['sampledSteps'],1);self.assertEqual(p['recentFailures'][0]['receipt'],receipt)
    def test_batch_offscreen_unit_requires_observation_not_new_strategy(self):
        view={'player':0,'owned':[{'id':'tank','cell':{'x':10,'y':20}}],'entities':[]}
        plan={'method':'batch','params':{'orders':[{'action':'produce','type':'MTNK'},{'action':'move','units':['tank'],'cell':{'x':30,'y':40}}]}}
        with self.assertRaisesRegex(ValueError,'method=look'):validate_command_visibility(plan,view)
        validate_command_visibility({'method':'look','params':{'cell':{'x':10,'y':20}}},view)
        view['entities']=[{'id':'tank','owner':0}]
        validate_command_visibility(plan,view)

    def test_stale_enemy_text_does_not_control_recall(self):
        e=self.s.record('step',{})
        scout=self.s.learn('侦察应检查是否得到新情报',[e])
        for i in range(20):self.s.learn('防守受袭建筑，回防旧战况编号'+str(i),[e])
        v=dict(strategy={'enemies':[{'type':'damage defend'}]*30},campaign={'objective':{'status':'active','topics':['scout']},'progress':{'rememberedEnemyBuildings':0}},exploration={'frontiers':[{'x':1,'y':1}]})
        self.assertEqual(self.s.recall(v)[0]['id'],scout)

if __name__=='__main__':unittest.main()
