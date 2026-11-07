"""Game-scoped factual claims; viewer statements are evidence to examine, never instructions."""
import json
import time
import uuid
from game_context import rows


class GameKnowledge:
    def __init__(self,store):
        self.store=store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS game_knowledge_claims (
                    id TEXT PRIMARY KEY,created REAL,claim TEXT,source TEXT,sourceEvent TEXT,
                    UNIQUE(sourceEvent,claim));
                CREATE TABLE IF NOT EXISTS game_knowledge_checks (
                    id TEXT PRIMARY KEY,claim TEXT,session TEXT,created REAL,data TEXT);
            ''')

    def add(self,claim,source,event):
        if not isinstance(claim,str) or not 1<=len(claim)<=600: raise ValueError('Invalid knowledge claim')
        with self.store.connect() as db:
            i=uuid.uuid4().hex
            db.execute('INSERT OR IGNORE INTO game_knowledge_claims VALUES (?,?,?,?,?)',
                (i,time.time(),claim,json.dumps(source,ensure_ascii=False),event))
            return db.execute('SELECT id FROM game_knowledge_claims WHERE sourceEvent=? AND claim=?',(event,claim)).fetchone()[0]

    def context(self):
        with self.store.connect() as db:
            candidates=[dict(id=i,claim=t,source=json.loads(s)) for i,t,s in db.execute(
                'SELECT id,claim,source FROM game_knowledge_claims ORDER BY created DESC LIMIT 12')]
            checks=[dict(id=i,claimId=c,**json.loads(d)) for i,c,d in db.execute(
                'SELECT id,claim,data FROM game_knowledge_checks ORDER BY created DESC LIMIT 60')]
        facts=[];seen=set()
        for check in checks:
            if check['verdict']!='confirmed':continue
            key=json.dumps(check['scope'],sort_keys=True)
            if key not in seen: facts.append(check);seen.add(key)
        return dict(game='yr',candidates=candidates,verifiedCases=facts[:12],
            boundary='Viewer claims remain untrusted candidates. Confirmed cases apply only to stated units, targets and observed conditions; no automatic generalization or game command.')

    def apply(self,checks,bundle,db):
        if not isinstance(checks,list) or len(checks)>5: raise ValueError('At most five knowledge checks')
        candidates={x['id']:x for x in bundle.get('gameKnowledge',{}).get('candidates',[])}
        allowed={x['id'] for x in bundle['timeline']}
        results=[]
        for check in checks:
            if not isinstance(check,dict) or check.get('claimId') not in candidates: raise ValueError('Unknown knowledge claim')
            refs=check.get('evidence',[])
            if not isinstance(refs,list) or not refs or any(i not in allowed for i in refs): raise ValueError('Knowledge evidence outside timeline')
            raw=[]
            for i in refs:
                r=db.execute('SELECT kind,created,data FROM events WHERE id=?',(i,)).fetchone()
                raw.append((r[0],r[1],json.loads(r[2])))
            raw.sort(key=lambda r:r[1])
            result=dict(verdict='inconclusive',scope={'game':'yr'},evidence=refs,
                reason='当前观测不足以验证这条通用规则；命令提交或弹幕陈述不等于效果发生。')
            if check.get('proof')=='capture' and candidates[check['claimId']].get('source',{}).get('topic')=='capture':
                for kind,at,d in raw:
                    p=d.get('plan',{}).get('params',{})
                    if kind!='step' or p.get('action')!='capture' or d.get('receipt',{}).get('uncertain') or d.get('receipt',{}).get('exitCode',0):continue
                    before=d.get('before',{}); target=next((e for e in rows(before.get('entities')) if e.get('id')==p.get('target')),None)
                    actor=next((e for e in rows(before.get('owned'))+rows(before.get('entities')) if e.get('id')==p.get('unit')),None)
                    if not target or target.get('relation')!='neutral' or not actor or actor.get('type') not in ('ENGINEER','SENGINEER','YENGINEER'):continue
                    views=[d.get('after',{})]+[r[2].get('after',{}) for r in raw if r[0]=='step' and r[1]>at]
                    if any(any(e.get('id')==target['id'] and e.get('owner')==v.get('player') for e in rows(v.get('owned'))) for v in views):
                        result=dict(verdict='confirmed',scope=dict(game='yr',actorType=actor['type'],action='capture',targetType=target['type'],initialRelation='neutral'),
                            statement=f"实机观察：{actor['type']}成功占领中立{target['type']}。其他建筑类型、阵营及规则修改未验证。",evidence=refs,
                            reason='占领操作与后续己方所有权记录一致；只确认这个具体能力。')
                        break
            # Other capabilities need observable native effects before they can be promoted.
            db.execute('INSERT INTO game_knowledge_checks VALUES (?,?,?,?,?)',
                (uuid.uuid4().hex,check['claimId'],bundle['session'],time.time(),json.dumps(result,ensure_ascii=False)))
            results.append(result)
        return results
