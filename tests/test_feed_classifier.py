import time
import unittest
from eddie.feed_classifier import FeedClassifier
from eddie import api
from fastapi.testclient import TestClient

class FeedAdapterTests(unittest.TestCase):
    def test_unknown_demo_event_enters_real_worker_and_cache(self):
        asked=[]; published=[]
        adapter=FeedClassifier(lambda facts:asked.append(facts) or '{"label":"tracker","confidence":0.9,"reason":"Demo telemetry"}',published.append)
        try:
            event={'id':1,'type':'query','ts':int(time.time()*1000),'device_mac':'demo-tv','device':'Demo TV','domain':'demo.example','qtype':'A','action':'flag','source':'default','reason':'Unknown'}
            self.assertEqual(adapter.ingest([event]),1)
            self.assertEqual(adapter.ingest([event]),0)
            adapter.worker.tick()
            for future in adapter.worker.pending.values(): future.result(timeout=2)
            adapter.worker.tick()
            self.assertEqual(len(asked),1)
            self.assertEqual(asked[0]['device'],'Demo TV')
            self.assertEqual(published[0]['domain'],'demo.example')
            adapter.worker.tick()
            self.assertFalse(adapter.worker.pending)
        finally: adapter.close()

    def test_verdicts_survive_browser_reconnect_without_changing_query_counts(self):
        old=dict(api.ai_verdicts)
        api.ai_verdicts.clear()
        try:
            with TestClient(api.app) as client:
                verdict={'domain':'demo.example','label':'needed','confidence':.9,'reason':'Demo content','ts':1000}
                client.post('/ai/verdict',json=verdict).raise_for_status()
                self.assertEqual(client.get('/ai/verdicts').json()[0]['label'],'needed')
                with client.websocket_connect('/ws/events') as ws:
                    self.assertEqual(ws.receive_json()['ai_verdicts'][0]['domain'],'demo.example')
        finally:
            api.ai_verdicts.clear();api.ai_verdicts.update(old)


class DemoDecisionTests(unittest.TestCase):
    def test_real_cached_verdict_changes_later_demo_decision(self):
        from scripts.fake_traffic import streams, decide
        stream=next(s for s in streams() if s.source=='default')
        cache={stream.domain:{'label':'tracker','confidence':.9,'reason':'Telemetry'}}
        self.assertEqual(decide(stream,False,cache)[:2],('block','ai'))
        self.assertEqual(decide(stream,True,cache)[1],'home_mode')
        cache[stream.domain]['confidence']=.2
        self.assertEqual(decide(stream,False,cache)[:2],('flag','ai'))
