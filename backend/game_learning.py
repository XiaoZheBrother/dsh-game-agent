"""Append-only evidence and model-authored, revisable gameplay hypotheses."""
import json
import re
import time
import uuid
from collections import Counter
from contextlib import nullcontext
from game_knowledge import GameKnowledge
from lesson_lifecycle import LessonLifecycle
from game_context import rows, receipt_summary

TOPICS = {
    'economy': ('经济','矿','油','资金','cash','CAOILD','CMIN','HARV','engineer','工程师'),
    'scout': ('侦察','迷雾','探索','寻敌','scout','frontier'),
    'combat': ('进攻','坦克','敌基地','集结','attack','rally','MTNK'),
    'defense': ('防守','回防','受袭','受损','defend','damage'),
    'build': ('建造','摆放','建筑','电厂','兵营','工厂','place','build','GACNST'),
    'movement': ('移动','抵达','路径','停滞','move','stalled'),
}


def tags(text):
    text = str(text).lower()
    return {key for key, words in TOPICS.items() if any(w.lower() in text for w in words)}


def normalized(text):
    return re.sub(r'\s+', '', text).lower()


def similar(a, b):
    def grams(t):
        t = normalized(t)
        return {t[i:i+2] for i in range(len(t)-1)}
    x, y = grams(a), grams(b)
    return bool(x and y) and len(x & y)/len(x | y) > .72


def snapshot(view):
    units = rows(view.get('owned',view.get('entities')))
    units = [e for e in units if e.get('owner') == view.get('player')]
    return dict(frame=view.get('frame'),cash=view.get('cash'),
        counts=dict(Counter(e.get('type') for e in units)),
        ownedOil=[e['id'] for e in units if e.get('type')=='CAOILD'],
        production=view.get('production',[]),
        phase=view.get('strategy',{}).get('phase'),
        objective=view.get('strategy',{}).get('objective'),
        baseAlert=view.get('tactics',{}).get('baseAlert'))


def event_summary(e):
    d=e['data']; out=dict(id=e['id'],created=e['created'],kind=e['kind'])
    if e['kind'] != 'step':
        out['data']=d; return out
    before, after=d.get('before',{}),d.get('after',{})
    p=d.get('plan',{}); params=p.get('params',{})
    selected=set(params.get('units',[])+([params['unit']] if params.get('unit') else []))
    if params.get('target'): selected.add(params['target'])
    for order in params.get('orders',[]):
        selected.update(order.get('units',[]))
        selected.update(order[k] for k in ('unit','target') if order.get(k))
    def tracked(v):
        return [{k:x.get(k) for k in ('id','type','cell','health','owner','relation','mission')}
                for x in rows(v.get('owned'))+rows(v.get('entities')) if x.get('id') in selected][:16]
    out.update(plan=p,receipt=receipt_summary(d.get('receipt',{})),
        before=snapshot(before),after=snapshot(after),trackedBefore=tracked(before),trackedAfter=tracked(after),
        adoptedLessonIds=d.get('adoptedLessonIds',[]),registeredTests=d.get('lessonTests',[]),expectedOutcome=d.get('expectedOutcome'),
        origin='local-controller' if d.get('localSkill') else 'model')
    # Native execution and locally replaced placement cells never belong to the model.
    fields=[] if d.get('localSkill') else ['method']
    if not d.get('localSkill'):
        fields += ['params.'+k for k in params if k not in ('cells','orders','onRejected')]
        for index, order in enumerate(params.get('orders',[])):
            fields += [f'params.orders.{index}.{k}' for k in order if k!='cells']
    if not d.get('localSkill') and p.get('placementPolicy') == 'model-selected-v1' and 'cells' in params:
        fields.append('params.cells')
    out['modelControlledFields']=fields
    out['toolControlledEffects']=['execution','pathfinding','combatResolution'] + ([] if 'params.cells' in fields else ['placementCells'])
    out['campaign']=before.get('campaign')
    out['objectiveDecision']=p.get('objective')
    return out


class Learning:
    def __init__(self, store):
        self.store=store
        self.knowledge=GameKnowledge(store)
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS lesson_details (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lesson_evaluations (
                    id TEXT PRIMARY KEY,lesson TEXT,step TEXT,session TEXT,created REAL,data TEXT,
                    UNIQUE(lesson,step));
                CREATE TABLE IF NOT EXISTS evaluation_audits (id TEXT PRIMARY KEY,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evaluation_deferrals (
                    step TEXT,lesson TEXT,reviewed REAL,evidenceThrough REAL,PRIMARY KEY(step,lesson));
            ''')

    def all(self):
        with self.store.connect() as db:
            records=db.execute("SELECT l.id,l.text,l.evidence,l.status,d.data FROM lessons l LEFT JOIN lesson_details d ON d.id=l.id WHERE l.status='candidate' ORDER BY l.created DESC").fetchall()
            validated=db.execute('SELECT e.lesson,COALESCE(a.data,e.data) FROM lesson_evaluations e LEFT JOIN evaluation_audits a ON a.id=e.id').fetchall()
        scores={}
        for lesson,raw in validated:
            v=json.loads(raw)
            if v.get('policyVersion')==2 and v.get('attribution')=='model-decision':
                counts=scores.setdefault(lesson,Counter()); counts[v['verdict']]+=1
        signals=LessonLifecycle(self.store).signals()
        result=[]
        for i,t,e,s,m in records:
            item=dict(id=i,text=t,evidence=json.loads(e),status=s,**(json.loads(m) if m else {}))
            item.update(supported=scores.get(i,{}).get('supported',0),contradicted=scores.get(i,{}).get('contradicted',0))
            signal=signals.get(i)
            if signal:
                item['reviewStatus']=signal['state']
                item['counterexampleCount']=len(signal['cases'])
                item['counterexampleSessions']=len(signal['sessions'])
                item['reviewWarning']='已有反例，采用前检查适用条件；不是已证实规则'
            result.append(item)
        return result

    def recall(self, view=None, limit=12):
        view=view or {}
        # Raw unit type lists and stale enemy history are not the current question.
        campaign=view.get('campaign',{}); objective=campaign.get('objective') or {}
        context=set(objective.get('topics',[])) if objective.get('status')=='active' else set()
        progress=campaign.get('progress',{})
        if progress.get('rememberedEnemyBuildings')==0 and view.get('exploration',{}).get('frontiers'): context.add('scout')
        if progress.get('loopingUnitIds') or progress.get('recentFailures'): context.add('movement')
        primary={'defend':'defense','restore-economy':'economy','establish-base':'build',
                 'scout':'scout','attack':'combat','rally':'combat'}.get(view.get('strategy',{}).get('phase'))
        if primary: context.add(primary)
        if view.get('tactics',{}).get('baseAlert'): primary='defense'; context.add('defense')
        if (view or {}).get('cash',10000)<1500: context.add('economy')
        candidates=[x for x in self.all() if not x.get('supersededBy') and not x.get('retired') and x.get('reviewStatus')!='suspended']
        def score(x):
            topics=set(x.get('tags') or tags(x['text']))
            return 12*(primary in topics)+8*bool(topics & context)+bool(x.get('when') and x.get('expect'))+min(x.get('supported',0),2)-min(x.get('contradicted',0),2)
        candidates.sort(key=score,reverse=True)
        selected=[]; groups=Counter()
        # Reserve a relevant slot per live question before filling the remaining budget.
        for topic in sorted(context):
            best=next((x for x in candidates if topic in set(x.get('tags') or tags(x['text'])) and x not in selected),None)
            if best and len(selected)<limit:
                selected.append(best); groups[tuple(sorted(best.get('tags') or tags(best['text'])))]+=1
        for x in candidates:
            topic=tuple(sorted(x.get('tags') or tags(x['text'])))
            if len(selected)>=limit: break
            if x in selected or groups[topic]>=2 or any(similar(x['text'],s['text']) for s in selected): continue
            selected.append(x); groups[topic]+=1
            if len(selected)>=limit: break
        return selected

    def bundle(self, session):
        with self.store.connect() as db:
            records=db.execute("""SELECT id,kind,created,data FROM events WHERE
              COALESCE(json_extract(data,'$.before.sessionId'),json_extract(data,'$.session'),
                       json_extract(data,'$.sessionId'))=? ORDER BY created""",(session,)).fetchall()
            evaluated={(r[0],r[1]) for r in db.execute('''SELECT e.step,e.lesson FROM lesson_evaluations e
                LEFT JOIN evaluation_audits a ON a.id=e.id WHERE e.session=?
                AND json_extract(COALESCE(a.data,e.data),'$.policyVersion')=2''',(session,))}
            prior=[dict(id=i,assessment=json.loads(raw)) for i,raw in db.execute('''SELECT e.id,e.data
                FROM lesson_evaluations e LEFT JOIN evaluation_audits a ON a.id=e.id
                WHERE e.session=? AND a.id IS NULL AND COALESCE(json_extract(e.data,'$.policyVersion'),0)<2 LIMIT 20''',(session,))]
            deferred={(s,l):(r,t) for s,l,r,t in db.execute('SELECT step,lesson,reviewed,evidenceThrough FROM evaluation_deferrals')}
        events=[dict(id=i,kind=k,created=t,data=json.loads(d)) for i,k,t,d in records]
        steps=[e for e in events if e['kind']=='step']
        if not steps: raise ValueError('还没有可复盘的实机记录')
        # Sample the whole match, not just its tail; retain every pending adoption up to a bounded batch.
        selected={e['id']:e for e in steps[-12:]}
        for i in range(min(24,len(steps))):
            e=steps[round(i*(len(steps)-1)/max(1,min(24,len(steps))-1))]; selected[e['id']]=e
        evidence_through=max([steps[-1]['created']]+[e['created'] for e in events if e['kind']=='native-game-end'])
        pairs=[(e,i) for e in steps for i in e['data'].get('adoptedLessonIds',[]) if (e['id'],i) not in evaluated
               and deferred.get((e['id'],i),(0,0))[1]<evidence_through]
        pairs.sort(key=lambda pair:(deferred.get((pair[0]['id'],pair[1]),(0,0))[0],pair[0]['created']))
        pairs=pairs[:6]
        pending=list({e['id']:e for e,i in pairs}.values())
        for e in pending:
            selected[e['id']]=e
            # Include immediate and delayed followups, rather than forcing a conclusion from a far-future sample.
            index=steps.index(e)
            for offset in (1,2,4):
                if index+offset<len(steps): selected[steps[index+offset]['id']]=steps[index+offset]
        important=[e for e in events if e['kind'] in ('oil-captured','scout-finished','strategy-transition','native-game-end','invalid-plan','failure')]
        # Preserve milestones across the match even if recent scouting creates many events.
        for kind in {e['kind'] for e in important}:
            group=[e for e in important if e['kind']==kind]
            for i in range(min(10,len(group))):
                e=group[round(i*(len(group)-1)/max(1,min(10,len(group))-1))]; selected[e['id']]=e
        timeline=sorted(selected.values(),key=lambda e:e['created'])
        latest=steps[-1]['data'].get('after',{})
        known={x['id']:x for x in self.recall(latest)}
        needed={i for e in pending for i in e['data']['adoptedLessonIds']}
        for x in self.all():
            if x['id'] in needed: known[x['id']]=x
        return dict(session=session,totalSteps=len(steps),sampledSteps=sum(e['kind']=='step' for e in timeline),
            timeline=[event_summary(e) for e in timeline],lessons=list(known.values()),
            pendingApplications=[e['id'] for e in pending],
            pendingTests=[dict(stepId=e['id'],lessonId=i) for e,i in pairs],evidenceThrough=evidence_through,
            validEvidenceIds=[e['id'] for e in timeline],
            priorEvaluations=prior,gameKnowledge=self.knowledge.context(),
            boundary='Sampled whole-match timeline. Missing events are not proof of absence. Local controller actions are not learned model decisions; adoption is model self-report, not causal proof.')

    def apply_partial(self, result, bundle, db):
        """Retain accepted items; rejected claims stay untrusted and auditable."""
        rejected=[]; accepted=0
        limits={'lessons':5,'evaluations':12,'retire':5,'knowledgeChecks':5}
        for section,limit in limits.items():
            items=result.get(section,[])
            if not isinstance(items,list) or len(items)>limit:
                rejected.append(dict(section=section,error='Unbounded reflection: invalid item count'));continue
            for index,item in enumerate(items):
                db.execute('SAVEPOINT reflection_item')
                try:
                    self.apply({section:[item]},bundle,db=db)
                    db.execute('RELEASE reflection_item');accepted+=1
                except (ValueError,TypeError,KeyError) as exc:
                    db.execute('ROLLBACK TO reflection_item');db.execute('RELEASE reflection_item')
                    rejected.append(dict(section=section,index=index,error=str(exc)[:600]))
        if not accepted and not rejected:
            self.apply({},bundle,db=db)
        if rejected:
            db.execute('INSERT INTO events VALUES (?,?,?,?)',(uuid.uuid4().hex,'reflection-items-rejected',time.time(),
                json.dumps(dict(session=bundle['session'],rejected=rejected,proposal=result),ensure_ascii=False)))
        return dict(accepted=accepted,rejected=rejected)

    def apply(self, result, bundle, db=None):
        """Validate all references first; commit one transaction, preserving original rows."""
        if not isinstance(result,dict): raise ValueError('Invalid reflection')
        proposals=result.get('lessons',[]); evaluations=result.get('evaluations',[])
        retire=result.get('retire',[])
        if not isinstance(proposals,list) or len(proposals)>5 or not isinstance(evaluations,list) or len(evaluations)>12:
            raise ValueError('Unbounded reflection')
        if not isinstance(retire,list) or len(retire)>5: raise ValueError('Unbounded retirements')
        evaluations=[dict(v) if isinstance(v,dict) else v for v in evaluations]
        evidence={e['id']:e for e in bundle['timeline']}; known={x['id']:x for x in bundle['lessons']}
        def refs(ids):
            if not isinstance(ids,list) or not ids or any(not isinstance(i,str) or i not in evidence for i in ids):
                invalid=[i for i in ids if not isinstance(i,str) or i not in evidence][:4] if isinstance(ids,list) else ids
                raise ValueError('Reflection evidence outside supplied match timeline: '+str(invalid))
        for p in proposals:
            if not isinstance(p,dict) or not isinstance(p.get('text'),str) or not 1<=len(p['text'])<=1200:
                raise ValueError('Invalid lesson text')
            refs(p.get('evidence'))
            if not isinstance(p.get('tags',[]),list) or any(t not in TOPICS for t in p.get('tags',[])): raise ValueError('Invalid lesson tags')
            if any(not isinstance(p.get(k,''),str) or len(p.get(k,''))>600 for k in ('when','expect')): raise ValueError('Invalid lesson conditions')
            if not isinstance(p.get('supersedes',[]),list) or any(i not in known for i in p.get('supersedes',[])): raise ValueError('Invalid revision target')
        for v in evaluations:
            if not isinstance(v,dict): raise ValueError('Invalid evaluation')
            refs(v.get('evidence')); step=evidence.get(v.get('stepId'),{})
            if (v.get('lessonId') not in known or v.get('stepId') not in bundle['pendingApplications']
                or v['lessonId'] not in step.get('adoptedLessonIds',[])
                or v.get('verdict') not in ('supported','contradicted','inconclusive')
                or not isinstance(v.get('reason'),str) or not 1<=len(v['reason'])<=1200):
                raise ValueError('Evaluation requires a recorded adoption')
            if dict(stepId=v['stepId'],lessonId=v['lessonId']) not in bundle['pendingTests']:
                raise ValueError('Evaluate only the supplied pendingTests pairs')
            if v['verdict']!='inconclusive' and not any(evidence[i]['created']>step['created'] for i in v['evidence']):
                raise ValueError('Evaluation requires later outcome evidence')
            if v['verdict']!='inconclusive':
                if v.get('attribution') not in ('model-decision','tool-assisted','unknown'):
                    raise ValueError('Evaluation requires explicit attribution: model-decision/tool-assisted/unknown')
                registered=next((t for t in step.get('registeredTests',[]) if t.get('lessonId')==v['lessonId']),None)
                if v['attribution']=='model-decision' and (not registered or registered.get('field')!=v.get('testedField')
                    or v.get('testedField') not in step.get('modelControlledFields',[])):
                    # Clamp credit to provenance without fabricating an alternative conclusion.
                    # Preserve the proposal for audit; this assessment cannot boost model learning scores.
                    v.update(proposedAttribution='model-decision',attribution='unknown',
                        attributionAdjustment='No matching pre-registered model-controlled test; outcome claim retained but model credit withheld')
                if v['attribution']=='model-decision' and (not isinstance(v.get('decisionReason'),str) or not 1<=len(v['decisionReason'])<=600):
                    raise ValueError('Explain which model choice the outcome supports, separately from tool execution')
        for r in retire:
            if not isinstance(r,dict) or r.get('lessonId') not in known or not isinstance(r.get('reason'),str) or not 1<=len(r['reason'])<=1200:
                raise ValueError('Invalid retirement')
            refs(r.get('evidence'))
        ids=[]
        with (self.store.connect() if db is None else nullcontext(db)) as db:
            knowledge_results=self.knowledge.apply(result.get('knowledgeChecks',[]),bundle,db)
            existing={normalized(t):i for i,t in db.execute('SELECT id,text FROM lessons')}
            for p in proposals:
                key=normalized(p['text'])
                if key in existing: ids.append(existing[key]); continue
                i=uuid.uuid4().hex; existing[key]=i; ids.append(i)
                db.execute('INSERT INTO lessons VALUES (?,?,?,?,?)',(i,time.time(),p['text'],json.dumps(p['evidence']),'candidate'))
                detail={k:p.get(k,[]) if k=='tags' else p.get(k,'') for k in ('tags','when','expect')}
                detail['origin']='model-reflection'; detail['session']=bundle['session']
                db.execute('INSERT INTO lesson_details VALUES (?,?)',(i,json.dumps(detail,ensure_ascii=False)))
                for old in p.get('supersedes',[]):
                    row=db.execute('SELECT data FROM lesson_details WHERE id=?',(old,)).fetchone()
                    meta=json.loads(row[0]) if row else {}; meta['supersededBy']=i
                    db.execute('INSERT OR REPLACE INTO lesson_details VALUES (?,?)',(old,json.dumps(meta)))
            for v in evaluations:
                if v['verdict']=='inconclusive':
                    # Defer until new evidence, and rotate the batch so old unknowns cannot starve newer tests.
                    db.execute('INSERT OR REPLACE INTO evaluation_deferrals VALUES (?,?,?,?)',
                               (v['stepId'],v['lessonId'],time.time(),bundle['evidenceThrough']))
                    continue
                v=dict(v,policyVersion=2)
                inserted=db.execute('INSERT OR IGNORE INTO lesson_evaluations VALUES (?,?,?,?,?,?)',
                    (uuid.uuid4().hex,v['lessonId'],v['stepId'],bundle['session'],time.time(),json.dumps(v,ensure_ascii=False))).rowcount
                if not inserted:
                    old=db.execute('SELECT id,data FROM lesson_evaluations WHERE lesson=? AND step=?',(v['lessonId'],v['stepId'])).fetchone()
                    if json.loads(old[1]).get('policyVersion')!=2:
                        db.execute('INSERT OR IGNORE INTO evaluation_audits VALUES (?,?)',(old[0],json.dumps(v,ensure_ascii=False)))
            for r in retire:
                row=db.execute('SELECT data FROM lesson_details WHERE id=?',(r['lessonId'],)).fetchone()
                meta=json.loads(row[0]) if row else {}; meta['retired']=dict(r,session=bundle['session'])
                db.execute('INSERT OR REPLACE INTO lesson_details VALUES (?,?)',(r['lessonId'],json.dumps(meta,ensure_ascii=False)))
            audit=dict(session=bundle['session'],lessons=ids,evaluations=evaluations,knowledgeChecks=knowledge_results,
                retire=retire,totalSteps=bundle['totalSteps'],sampledSteps=bundle['sampledSteps'],evidenceIds=list(evidence),policyVersion=2)
            db.execute('INSERT INTO events VALUES (?,?,?,?)',(uuid.uuid4().hex,'learning-reflection',time.time(),json.dumps(audit,ensure_ascii=False)))
        return ids
