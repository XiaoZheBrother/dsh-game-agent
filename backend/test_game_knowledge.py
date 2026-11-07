import tempfile
import unittest
from pathlib import Path
from game_director import GameStore,GameDirector
from test_game_director import Model,Adapter


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=GameStore(Path(self.tmp.name)/'db')
    def tearDown(self):self.tmp.cleanup()
    def evidence(self,success=True):
        unit=dict(id='eng',type='ENGINEER',owner=0)
        oil=dict(id='oil',type='CAOILD',owner=2,relation='neutral')
        before=dict(sessionId='s',player=0,entities=[unit,oil],owned=[unit])
        after=dict(sessionId='s',player=0,entities=[],owned=[dict(oil,owner=0)] if success else [])
        return self.store.record('step',dict(before=before,after=after,localSkill=True,
            plan=dict(method='act',params=dict(action='capture',unit='eng',target='oil')),receipt={'exitCode':0}))
    def test_capture_confirms_narrow_case_not_all_buildings(self):
        k=self.store.learning.knowledge.add('工程师能占领所有建筑',{'kind':'viewer','topic':'capture'},'message1')
        e=self.evidence();b=self.store.learning.bundle('s')
        self.store.learning.apply({'knowledgeChecks':[dict(claimId=k,proof='capture',evidence=[e])]},b)
        case=self.store.learning.knowledge.context()['verifiedCases'][0]
        self.assertEqual(case['scope']['targetType'],'CAOILD');self.assertIn('其他建筑类型',case['statement'])
        self.assertEqual(self.store.learning.knowledge.context()['candidates'][0]['claim'],'工程师能占领所有建筑')
    def test_submitted_capture_without_owner_change_remains_unconfirmed(self):
        k=self.store.learning.knowledge.add('工程师能占油田',{'topic':'capture'},'m')
        e=self.evidence(False);b=self.store.learning.bundle('s')
        self.store.learning.apply({'knowledgeChecks':[dict(claimId=k,proof='capture',evidence=[e])]},b)
        self.assertEqual(self.store.learning.knowledge.context()['verifiedCases'],[])
    def test_wrong_capability_cannot_be_proven_by_oil_capture(self):
        k=self.store.learning.knowledge.add('步兵能部署',{'topic':'deploy'},'m')
        e=self.evidence();b=self.store.learning.bundle('s')
        self.store.learning.apply({'knowledgeChecks':[dict(claimId=k,proof='capture',evidence=[e])]},b)
        self.assertEqual(self.store.learning.knowledge.context()['verifiedCases'],[])
    def test_chat_knowledge_does_not_issue_game_orders(self):
        model=Model();model.classify=lambda text:dict(kind='chat',knowledgeClaims=[dict(text='步兵能部署',topic='deploy')])
        adapter=Adapter();director=GameDirector(self.store,model,adapter)
        director.submit('viewer','美国大兵可以部署',uid='123')
        director.tick()
        self.assertEqual(adapter.writes,[])
        self.assertEqual(len(self.store.learning.knowledge.context()['candidates']),1)

if __name__=='__main__':unittest.main()
