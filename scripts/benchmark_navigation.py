"""Offline prepared-view benchmark. Telegram calls are simulated; no messages are sent."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

from scripts.preview_entry_report import sample_assessment,NOW
from buysignal.telegram_bot import interactive,transport
from buysignal.telegram_bot.snapshots import SnapshotStore


def main():
    calls={'uploads':0,'api_calls':0}
    class Response:
        status_code=200
        def raise_for_status(self): pass
        def __init__(self,file_id):self.file_id=file_id
        def json(self):return {'ok':True,'result':{'message_id':99,'photo':[{'file_id':self.file_id}]}}
    def post(url,**kw):
        calls['api_calls']+=1
        files=kw.get('files')
        if files:
            calls['uploads']+=1
            png=next(iter(files.values()))[1]
            return Response('offline:'+hashlib.sha256(png).hexdigest())
        return Response(kw.get('json',{}).get('media',{}).get('media','offline:existing'))
    names=['market_dip','index_NASDAQ','index_DOW','index_EURO50','market_info','vix','fed_rate','fed_changes','margin_debt','sector','eps_info','credit','yield_curve']
    with TemporaryDirectory(prefix='buysignal-prepared-benchmark-') as folder:
        store=SnapshotStore(folder)
        start=time.perf_counter(); token=store.save(sample_assessment(),NOW)
        preparation=time.perf_counter()-start
        with patch.dict('os.environ',{'TELEGRAM_CHAT_ID':'0','TELEGRAM_BOT_TOKEN':'offline-only'}), patch.object(transport.requests,'post',post), patch.object(transport,'answer_callback',lambda *a:None), patch.object(transport.time,'sleep',lambda *a:None):
            def cycle():
                for name in names:
                    interactive.handle_callback({'id':'benchmark','data':f'r:{token}:{name}','message':{'message_id':99}},store)
            cycle(); cold_uploads=calls['uploads']
            start=time.perf_counter()
            for _ in range(10):cycle()
            elapsed=time.perf_counter()-start
        print(json.dumps({
            'one_time_preparation_seconds':round(preparation,3),
            'prepared_chart_files':len(list(Path(folder).rglob('*.png'))),
            'snapshot_disk_bytes':sum(p.stat().st_size for p in Path(folder).rglob('*') if p.is_file()),
            'local_navigation_ms_per_click':round(elapsed/(10*len(names))*1000,3),
            'first_cycle_uploads':cold_uploads,'repeat_cycle_uploads':calls['uploads']-cold_uploads,
            'note':'Offline only; excludes Telegram network latency and delivery pacing.'},indent=2))


if __name__=='__main__':main()
