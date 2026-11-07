"""Evidence-triggered lesson review. Never issues game commands or deletes history."""
import json
import time
import uuid


class LessonLifecycle:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS lesson_reviews (
                lesson TEXT PRIMARY KEY, state TEXT, due REAL, attempts INTEGER,
                lease TEXT, leaseUntil REAL, resolvedThrough REAL DEFAULT 0, error TEXT)''')

    def status(self):
        with self.store.connect() as db:
            return [dict(lessonId=i,state=s,attempts=a,nextAttempt=d,error=e)
                    for i,s,a,d,e in db.execute('SELECT lesson,state,attempts,due,error FROM lesson_reviews ORDER BY due DESC LIMIT 20')]

    def signals(self):
        with self.store.connect() as db:
            reviews = {r[0]: r[1:] for r in db.execute('SELECT lesson,state,resolvedThrough FROM lesson_reviews')}
            records = db.execute('''SELECT v.lesson,v.session,v.created,COALESCE(a.data,v.data),e.data
                FROM lesson_evaluations v JOIN events e ON e.id=v.step
                LEFT JOIN evaluation_audits a ON a.id=v.id ORDER BY v.created''').fetchall()
            events = {i: (t, session) for i,t,session in db.execute("SELECT id,created,COALESCE(json_extract(data,'$.before.sessionId'),json_extract(data,'$.session')) FROM events")}
        result = {}
        for lesson, session, at, raw, stepraw in records:
            v, step = json.loads(raw), json.loads(stepraw)
            if at <= reviews.get(lesson, ('', 0))[1]: continue
            receipt = step.get('receipt', {})
            if v.get('policyVersion') != 2 or v.get('verdict') != 'contradicted' or v.get('attribution') != 'model-decision': continue
            if step.get('localSkill') or receipt.get('uncertain') or receipt.get('exitCode') != 0: continue
            if any(x.get('error') or x.get('rejected') or 'reject' in str(x.get('event','')).lower() for x in receipt.get('events',[]) if isinstance(x,dict)): continue
            test = next((t for t in step.get('lessonTests',[]) if t.get('lessonId') == lesson), None)
            if not test or test.get('field') != v.get('testedField'): continue
            step_at = events.get(v.get('stepId'), (float('inf'), {}))[0]
            later = {i for i in v.get('evidence',[]) if i in events and events[i][0] > step_at
                     and events[i][1] == session}
            if not later: continue
            # Repeated orders to another cell/unit are not independent experiments.
            def clean(value):
                if isinstance(value, dict): return {k:clean(x) for k,x in value.items() if k not in ('unit','units','cell','cells','say')}
                if isinstance(value, list): return [clean(x) for x in value]
                return value
            key = (session, json.dumps(clean({k:step.get('plan',{}).get(k) for k in ('method','params')}),sort_keys=True), test['field'])
            entry = result.setdefault(lesson, dict(cases=[], keys=set(), refs=set(), sessions=set(), through=0))
            if key in entry['keys'] or later & entry['refs']: continue
            entry['keys'].add(key); entry['refs'].update(later); entry['sessions'].add(session)
            entry['cases'].append(v); entry['through'] = max(entry['through'], at)
        for lesson, entry in result.items():
            entry['state'] = 'suspended' if len(entry['cases']) >= 3 and len(entry['sessions']) >= 2 else 'disputed'
        return result

    def run_one(self, model):
        if not hasattr(model, 'review_lesson'): return False
        signals = self.signals(); now = time.time()
        with self.store.connect() as db:
            for lesson, signal in signals.items():
                if signal['state'] == 'suspended':
                    db.execute("INSERT OR IGNORE INTO lesson_reviews VALUES (?,'pending',?,0,NULL,0,0,NULL)",(lesson,now))
                    db.execute("UPDATE lesson_reviews SET state='pending',due=?,attempts=0 WHERE lesson=? AND state='done'",(now,lesson))
            db.execute('SELECT 1')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT lesson,attempts FROM lesson_reviews WHERE (state='pending' AND due<=?) OR (state='running' AND leaseUntil<=?) ORDER BY due LIMIT 1",(now,now)).fetchone()
            if not row: return False
            lesson, attempts = row; token = uuid.uuid4().hex
            db.execute("UPDATE lesson_reviews SET state='running',lease=?,leaseUntil=? WHERE lesson=?",(token,now+300,lesson))
            original = db.execute('SELECT text,evidence FROM lessons WHERE id=?',(lesson,)).fetchone()
            meta = json.loads(db.execute('SELECT data FROM lesson_details WHERE id=?',(lesson,)).fetchone()[0] or '{}') if db.execute('SELECT 1 FROM lesson_details WHERE id=?',(lesson,)).fetchone() else {}
        try:
            signal = signals.get(lesson)
            if not signal: raise ValueError('No current counterexamples')
            refs = set().union(*(set(v['evidence']) | {v['stepId']} for v in signal['cases']))
            from game_learning import event_summary
            with self.store.connect() as db:
                evidence = [event_summary(dict(id=i,kind=k,created=t,data=json.loads(d))) for i,k,t,d in db.execute('SELECT id,kind,created,data FROM events WHERE id IN ('+','.join('?' for _ in refs)+')',list(refs))]
            bundle = dict(lessonId=lesson,text=original[0],metadata=meta,counterexamples=signal['cases'],evidence=evidence)
            response = model.review_lesson(bundle)
            if not isinstance(response,dict) or response.get('action') not in ('revise','retire','retain'): raise ValueError('Choose revise, retire or retain')
            if not isinstance(response.get('reason'),str) or not 1 <= len(response['reason']) <= 1200: raise ValueError('Review requires reason')
            cited = response.get('evidence')
            if not isinstance(cited,list) or not cited or any(i not in refs for i in cited): raise ValueError('Review requires supplied evidence')
            # Summaries of steps omit session in their before snapshot; use authoritative events.
            with self.store.connect() as db:
                cited_sessions = {session for (session,) in db.execute("SELECT COALESCE(json_extract(data,'$.before.sessionId'),json_extract(data,'$.session')) FROM events WHERE id IN ("+','.join('?' for _ in cited)+')',cited) if session}
            if len(cited_sessions)<2: raise ValueError('Special review must cite evidence from at least two matches')
            if response['action']=='revise':
                if any(not isinstance(response.get(k),str) or not 1<=len(response[k])<=1200 for k in ('text','when','expect')): raise ValueError('Revision requires text, when and expect')
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT lease FROM lesson_reviews WHERE lesson=?',(lesson,)).fetchone()[0] != token: return True
                current = db.execute('SELECT data FROM lesson_details WHERE id=?',(lesson,)).fetchone()
                meta = json.loads(current[0]) if current else {}
                audit = dict(lessonId=lesson,response=response,through=signal['through'])
                # Concurrent ordinary reflection may already have retired/replaced it.
                if not meta.get('retired') and not meta.get('supersededBy'):
                    if response['action']=='revise':
                        new = uuid.uuid4().hex
                        db.execute('INSERT INTO lessons VALUES (?,?,?,?,?)',(new,time.time(),response['text'],json.dumps(cited),'candidate'))
                        detail = dict(origin='model-special-review',tags=meta.get('tags',[]),when=response['when'],expect=response['expect'],revises=lesson)
                        db.execute('INSERT INTO lesson_details VALUES (?,?)',(new,json.dumps(detail,ensure_ascii=False)))
                        meta['supersededBy']=new; audit['replacement']=new
                    elif response['action']=='retire': meta['retired']=dict(reason=response['reason'],evidence=cited,origin='model-special-review')
                    meta['lastSpecialReview']=response
                    db.execute('INSERT OR REPLACE INTO lesson_details VALUES (?,?)',(lesson,json.dumps(meta,ensure_ascii=False)))
                db.execute('INSERT INTO events VALUES (?,?,?,?)',(uuid.uuid4().hex,'lesson-special-review',time.time(),json.dumps(audit,ensure_ascii=False)))
                db.execute("UPDATE lesson_reviews SET state='done',resolvedThrough=?,lease=NULL,leaseUntil=0,error=NULL WHERE lesson=?",(signal['through'],lesson))
        except Exception as exc:
            with self.store.connect() as db:
                db.execute("UPDATE lesson_reviews SET state=?,attempts=?,due=?,lease=NULL,leaseUntil=0,error=? WHERE lesson=? AND lease=?",('needs-attention' if attempts+1>=5 else 'pending',attempts+1,time.time()+min(600,15*4**min(attempts,3)),str(exc)[:600],lesson,token))
        return True
