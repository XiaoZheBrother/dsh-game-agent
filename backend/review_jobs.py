"""Persistent single-claim review jobs. This worker has no game-write interface."""
import json
import time
import uuid
from lesson_lifecycle import LessonLifecycle


class ReviewJobs:
    def __init__(self, store, model, clock=time.time):
        self.store,self.model,self.clock=store,model,clock
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS reflection_jobs (
                session TEXT PRIMARY KEY,state TEXT,requested REAL,due REAL,attempts INTEGER,
                lease TEXT,leaseUntil REAL,error TEXT,response TEXT)''')
            # Recover recent sessions with adoption evidence, including failed end-of-round reviews.
            sessions=db.execute("""SELECT json_extract(data,'$.before.sessionId'),max(created)
                FROM events WHERE kind='step' AND json_array_length(json_extract(data,'$.adoptedLessonIds'))>0
                GROUP BY 1 ORDER BY 2 DESC LIMIT 10""").fetchall()
            for session, watermark in sessions:
                db.execute("INSERT OR IGNORE INTO reflection_jobs VALUES (?,'pending',?,?,0,NULL,0,NULL,NULL)",
                           (session,watermark,self.clock()))

    def enqueue(self, session):
        if not session: raise ValueError('Review requires a session')
        now=self.clock()
        with self.store.connect() as db:
            db.execute("""INSERT INTO reflection_jobs VALUES (?,'pending',?,?,0,NULL,0,NULL,NULL)
                ON CONFLICT(session) DO UPDATE SET requested=excluded.requested,
                state=CASE WHEN state='running' THEN state ELSE 'pending' END,
                due=CASE WHEN state IN ('running','pending') THEN due ELSE excluded.due END,
                attempts=CASE WHEN state='needs-attention' THEN 0 ELSE attempts END""",(session,now,now))

    def status(self):
        with self.store.connect() as db:
            return [dict(session=s,state=t,attempts=a,nextAttempt=d,error=e)
                    for s,t,a,d,e in db.execute('SELECT session,state,attempts,due,error FROM reflection_jobs ORDER BY requested DESC LIMIT 10')]

    def run_one(self):
        if LessonLifecycle(self.store).run_one(self.model): return True
        now=self.clock(); lease=uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("""SELECT session,requested,attempts,error,response FROM reflection_jobs
                WHERE (state='pending' AND due<=?) OR (state='running' AND leaseUntil<=?)
                ORDER BY due LIMIT 1""",(now,now)).fetchone()
            if not row: return False
            session,watermark,attempts,error,previous=row
            db.execute("UPDATE reflection_jobs SET state='running',lease=?,leaseUntil=? WHERE session=?",(lease,now+300,session))
        response=None
        try:
            bundle=self.store.learning.bundle(session)
            if error:
                bundle['repairRequest']=dict(error=error,previousResponse=json.loads(previous) if previous else None,
                    instruction='Correct the rejected reflection using only supplied evidence. Respect item limits; unsupported attribution or missing later evidence must be inconclusive, never invented.')
            response=self.model.reflect(bundle)
            if not isinstance(response,dict): raise ValueError('Reflection must be an object')
            required={(t['stepId'],t['lessonId']) for t in bundle.get('pendingTests',[])}
            supplied={(v.get('stepId'),v.get('lessonId')) for v in (response.get('evaluations') or []) if isinstance(v,dict)} if isinstance(response.get('evaluations',[]),list) else set()
            # Fence expired workers before applying. The same transaction commits results and job completion.
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current=db.execute('SELECT lease,requested FROM reflection_jobs WHERE session=?',(session,)).fetchone()
                if current[0]!=lease: return True
                applied=self.store.learning.apply_partial(response,bundle,db=db)
                if required-supplied:
                    applied['rejected'].append(dict(section='evaluations',error='Address every pendingTests pair; use inconclusive when evidence is insufficient'))
                remaining=db.execute('''SELECT 1 FROM events e JOIN json_each(e.data,'$.adoptedLessonIds') j
                    LEFT JOIN lesson_evaluations v ON v.step=e.id AND v.lesson=j.value
                    LEFT JOIN evaluation_audits a ON a.id=v.id
                    LEFT JOIN evaluation_deferrals d ON d.step=e.id AND d.lesson=j.value
                    WHERE e.kind='step' AND json_extract(e.data,'$.before.sessionId')=?
                    AND COALESCE(json_extract(COALESCE(a.data,v.data),'$.policyVersion'),0)!=2
                    AND COALESCE(d.evidenceThrough,0)<? LIMIT 1''',(session,bundle['evidenceThrough'])).fetchone()
                state='pending' if current[1]>watermark or remaining else 'done'
                if applied['rejected']:
                    db.execute('UPDATE reflection_jobs SET state=?,due=?,attempts=?,lease=NULL,leaseUntil=0,error=?,response=? WHERE session=?',
                        ('needs-attention' if attempts+1>=5 else 'pending',self.clock()+min(600,15*4**min(attempts,3)),attempts+1,
                         json.dumps(applied['rejected'],ensure_ascii=False),json.dumps(response,ensure_ascii=False),session))
                else:
                    db.execute('UPDATE reflection_jobs SET state=?,due=?,attempts=0,lease=NULL,leaseUntil=0,error=NULL,response=NULL WHERE session=?',
                               (state,self.clock()+30,session))
            self.store.record('reflection-job-partial' if applied['rejected'] else 'reflection-job-completed',dict(session=session,watermark=watermark,accepted=applied['accepted'],rejected=applied['rejected']))
        except Exception as exc:
            attempts+=1; delay=min(600,15*4**min(attempts-1,3))
            message=str(exc)[:600]
            with self.store.connect() as db:
                db.execute("""UPDATE reflection_jobs SET state=?,attempts=?,due=?,lease=NULL,leaseUntil=0,error=?,response=?
                    WHERE session=? AND lease=?""",('needs-attention' if attempts>=5 else 'pending',attempts,self.clock()+delay,
                    message,json.dumps(response,ensure_ascii=False) if response is not None else previous,session,lease))
            self.store.record('reflection-job-failed',dict(session=session,attempt=attempts,error=message,
                response=response,nextAttempt=self.clock()+delay,needsAttention=attempts>=5))
        return True
