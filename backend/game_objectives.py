"""Model-owned objectives and observational progress, never strategic orders."""
import json
import math
from statistics import median
from game_context import rows, receipt_summary


TOPICS={'economy','build','combat','defense','scout','movement'}

def validate_command_visibility(plan,view):
    """The native bridge only accepts unit commands in its current entity view."""
    if 'owned' not in view or 'player' not in view:return
    params=plan.get('params',{})
    visible={e['id'] for e in rows(view.get('entities')) if e.get('owner')==view['player'] or e.get('relation')=='self'}
    owned={e['id']:e for e in rows(view.get('owned'))}
    for order in params.get('orders',[params]):
        ids=order.get('units',[])+([order['unit']] if order.get('unit') else [])
        missing=[i for i in ids if i not in visible]
        if missing:
            locations=[dict(id=i,cell=owned[i].get('cell')) for i in missing[:3] if i in owned]
            raise ValueError('Unit commands require current viewport IDs. First use method=look at an owned unit location, then reobserve before ordering. Missing='+str(missing[:3])+' locations='+str(locations))


class Objectives:
    def __init__(self,store): self.store=store

    def current(self,session):
        with self.store.connect() as db:
            row=db.execute("SELECT data FROM events WHERE kind='model-objective' AND json_extract(data,'$.session')=? ORDER BY created DESC LIMIT 1",(session,)).fetchone()
        return json.loads(row[0]) if row else None

    def context(self,view):
        session=view['sessionId']
        with self.store.connect() as db:
            records=db.execute("SELECT id,created,data FROM events WHERE kind='step' AND json_extract(data,'$.before.sessionId')=? ORDER BY created DESC LIMIT 16",(session,)).fetchall()
        records=[(i,t,json.loads(d)) for i,t,d in reversed(records)]
        goal=self.current(session)
        if not records: return dict(objective=goal,progress={'sampledSteps':0,'boundary':'No previous steps in this match'})
        first=records[0][2].get('before',{})
        commanded=set()
        for _,_,d in records:
            p=d.get('plan',{});params=p.get('params',{})
            for order in params.get('orders',[params]):
                if order.get('action') in ('move','attack-move','attack') or p.get('method')=='advance':
                    commanded.update(order.get('units',[]))
                    if order.get('unit'):commanded.add(order['unit'])
        snapshots=[d.get('before',{}) for _,_,d in records]+[view]
        units=[{e['id']:e for e in rows(v.get('owned')) if e.get('cell') and e['id'] in commanded} for v in snapshots]
        travel={}
        for a,b in zip(units,units[1:]):
            for i in a.keys() & b.keys():
                travel[i]=travel.get(i,0)+math.hypot(a[i]['cell']['x']-b[i]['cell']['x'],a[i]['cell']['y']-b[i]['cell']['y'])
        net={i:math.hypot(units[0][i]['cell']['x']-units[-1][i]['cell']['x'],units[0][i]['cell']['y']-units[-1][i]['cell']['y']) for i in units[0].keys() & units[-1].keys()}
        looping=[i for i,d in travel.items() if d>=12 and net.get(i,float('inf'))<d*.25]
        first_enemies={e['id'] for e in first.get('strategy',{}).get('enemies',[])}
        known=view.get('strategy',{}).get('enemies',[])
        fresh=[e for e in known if e['id'] not in first_enemies]
        failures=[]; destinations=[]; actions=[]
        for i,_,d in records:
            p=d.get('plan',{}); receipt=d.get('receipt',{})
            orders=p.get('params',{}).get('orders',[p.get('params',{})])
            for o in orders:
                actions.append(o.get('action',p.get('method')))
                if o.get('cell'): destinations.append(dict(stepId=i,cell=o['cell'],action=o.get('action',p.get('method')),rejected=bool(receipt.get('exitCode'))))
            if receipt.get('uncertain') or receipt.get('exitCode'):
                failures.append(dict(stepId=i,receipt=receipt_summary(receipt)))
        frontiers=view.get('exploration',{}).get('frontiers',[])
        # Spatially spread samples, not a prescribed destination or hidden-map information.
        samples=[]
        for c in frontiers:
            if not samples or all(math.hypot(c['x']-p['x'],c['y']-p['y'])>=12 for p in samples): samples.append(c)
            if len(samples)>=12:break
        return dict(objective=goal,progress=dict(sampledSteps=len(records),evidenceIds=[i for i,_,_ in records],
            windowSeconds=round(records[-1][1]-records[0][1]),cashBefore=first.get('cash'),cashNow=view.get('cash'),
            movingUnits=sum(d>2 for d in travel.values()),loopingUnitIds=looping[:24],
            medianNetDisplacement=round(median(net.values()),1) if net else None,
            newEnemySightings=len(fresh),newEnemyBuildings=sum(e.get('kind')=='building' for e in fresh),
            rememberedEnemyBuildings=sum(e.get('kind')=='building' for e in known),
            recentDestinations=destinations[-10:],recentFailures=failures[-4:],frontierSamples=samples,
            productionOrders=actions.count('produce'),
            boundary='Movement is not exploration or victory. New sightings are evidence, absence is not safety. Loop flag is a geometric hint, not an order. Frontier samples are choices; you choose objectives and routes.'))

    def validate(self,proposal,current,allowed):
        if not isinstance(proposal,dict) or proposal.get('action') not in ('set','continue','replace','complete'): raise ValueError('Return objective with action set/continue/replace/complete')
        action=proposal['action']; active=current and current.get('status')=='active'
        if action=='set' and active: raise ValueError('An objective is active; continue it or explain replacement')
        if action!='set' and not active: raise ValueError('No active objective; set one first')
        if action in ('set','replace'):
            if any(not isinstance(proposal.get(k),str) or not 1<=len(proposal[k])<=600 for k in ('objective','success','reason')): raise ValueError('Objective requires objective, success, reason')
            topics=proposal.get('topics')
            if not isinstance(topics,list) or not 1<=len(topics)<=3 or any(t not in TOPICS for t in topics): raise ValueError('Objective requires 1-3 valid topics')
        if action in ('replace','complete'):
            if not isinstance(proposal.get('reason'),str) or not 1<=len(proposal['reason'])<=600: raise ValueError('Explain objective transition')
            refs=proposal.get('evidenceIds')
            if not isinstance(refs,list) or not refs or any(i not in allowed for i in refs): raise ValueError('Objective transition requires supplied progress evidence IDs')

    def accept(self,proposal,current,session,step):
        if proposal['action']=='continue':return
        goal=dict(current or {})
        if proposal['action'] in ('set','replace'):
            goal.update(objective=proposal['objective'],success=proposal['success'],topics=proposal['topics'],status='active',startedStep=step)
        else:goal['status']='completed-by-model'
        goal.update(session=session,reason=proposal.get('reason'),evidenceIds=proposal.get('evidenceIds',[]),decisionStep=step)
        self.store.record('model-objective',goal)
