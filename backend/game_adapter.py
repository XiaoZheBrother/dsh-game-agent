"""Use the installed ra2ctl skill, never inject guest memory or arbitrary shell."""
import json
import os
import subprocess
import tempfile
from pathlib import Path
from game_director import validate_plan, UnsubmittedPlanError
from game_tactics import guard_targets


class Ra2Adapter:
    def __init__(self, skill, channel='1'):
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', channel):
            raise ValueError('Invalid channel')
        self.skill = Path(skill).resolve()
        from game_frames import GameFrames
        self.frames = GameFrames(channel)
        if not (self.skill/'scripts/ra2ctl.mts').is_file():
            raise ValueError('Missing ra2ctl skill')
        self.argv = ['node', str(self.skill/'node_modules/tsx/dist/cli.mjs'),
                     str(self.skill/'scripts/ra2ctl.mts'), '--channel', channel]
        # Process-lifetime OS lock: survives thread failures, releases on crash.
        self.claim = open(Path(tempfile.gettempdir())/f'dafeiyu-ra2-channel-{channel}.lock', 'a+b')
        self.claim.seek(0); self.claim.write(b'0'); self.claim.flush(); self.claim.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.claim.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.claim.close()
            raise ValueError('已有大肥鱼控制器占用该游戏频道')

    def call(self, method, params=None, raw=False):
        argv = self.argv + (['--raw'] if raw else []) + [method]
        if params is not None:
            argv += ['--params', json.dumps(params)]
        result = subprocess.run(argv, cwd=self.skill, capture_output=True, text=True,
                                encoding='utf8', timeout=40, shell=False)
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        if not rows:
            raise RuntimeError('游戏接口没有返回可核对的结果')
        return result.returncode, rows

    def observe(self):
        try:
            code, rows = self.call('observe', raw=True)
        except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError('无法读取当前战况') from exc
        if code or 'error' in rows[-1]:
            raise RuntimeError('无法读取当前战况')
        return rows[-1].get('result', rows[-1])

    def native_status(self):
        try:
            code,rows=self.call('ui.status',raw=True)
        except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError('无法确认原生对局状态') from exc
        if code or 'error' in rows[-1]: raise RuntimeError('无法确认原生对局状态')
        return rows[-1].get('result',rows[-1])

    def next_battle(self, status, session, command_id):
        code,rows=self.call('ui.nextBattle',{'expectedPageId':status['pageId'],
            'expectedSessionId':session,'replaceSession':True,'commandId':command_id},raw=True)
        if code or 'error' in rows[-1]: raise RuntimeError('下一局启动请求未确认')
        return rows[-1]

    def execute(self, method, params, session):
        validate_plan({'method':method, 'params':params})
        latest = self.observe()
        try:
            guard_targets({'method':method,'params':params}, latest)
        except ValueError as exc:
            raise UnsubmittedPlanError('Target changed before submission; reobserve instead of replaying the old attack') from exc
        if latest.get('sessionId') != session:
            raise RuntimeError('对局已改变，未提交操作')
        try:
            code, rows = self.call(method, {**params, 'sessionId':session})
        except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError):
            return {'uncertain':True, 'status':'结果未知，不能重发'}
        uncertainty = any('unknown' in str(r).lower() or 'inspect commandstatus' in str(r).lower() for r in rows)
        return {'exitCode':code, 'events':rows, 'uncertain':uncertainty,
                'status':'已提交，效果以回读为准' if code==0 else '操作被拒绝或部分失败'}

    def frame(self):
        return self.frames.frame()
