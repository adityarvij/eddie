import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from eddie import api, classifier, store

GOOD=json.dumps({'label':'tracker','confidence':.9,'reason':'Usage telemetry'})
class AsyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.conn=store.connect(str(Path(self.tmp.name)/'events.db'))
        self.workers=[]
    def tearDown(self):
        for worker in self.workers: worker.close()
        self.conn.close(); self.tmp.cleanup()
    def query(self,domain):
        with self.conn:
            self.conn.execute("INSERT INTO queries(ts,device_mac,domain,qtype,action,source,reason) VALUES (?,?,?,'A','flag','default','')",(int(time.time()*1000),'TV',domain))
    def worker(self,ask,publish):
        worker=classifier.AsyncClassifier(self.conn,ask,publish)
        self.workers.append(worker); return worker
    def finish(self,worker):
        for future in list(worker.pending.values()): future.result(timeout=2)
        worker.tick()
    def test_slow_inference_does_not_hold_tick_or_duplicate_domain(self):
        gate=threading.Event(); self.addCleanup(gate.set)
        self.query('one.example'); calls=[]; events=[]
        worker=self.worker(lambda q:(gate.wait(1),calls.append(q),GOOD)[-1],events.append)
        start=time.monotonic(); worker.tick(); worker.tick()
        self.assertLess(time.monotonic()-start,.1)
        self.assertEqual(len(worker.pending),1)
        gate.set(); self.finish(worker)
        self.assertEqual(len(events),1); self.assertEqual(len(calls),1)
        worker.tick(); self.assertFalse(worker.pending)
    def test_cached_verdict_retries_event_without_repeating_inference(self):
        self.query('one.example'); events=[]; calls=[]
        def publish(event):
            events.append(event)
            if len(events)==1: raise OSError('down')
        worker=self.worker(lambda q:calls.append(q) or GOOD,publish)
        worker.tick()
        with self.assertLogs('eddie.classifier',level='WARNING'): self.finish(worker)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ai_verdicts').fetchone()[0],1)
        worker.event_retry_at=0; worker.tick()
        self.assertEqual(len(events),2); self.assertEqual(len(calls),1)
        self.assertFalse(worker.outbox)
    def test_unhashable_or_nonfinite_output_is_rejected(self):
        for label in ([],{},None):
            self.assertIsNone(classifier.parse_verdict(json.dumps({'label':label,'confidence':.9,'reason':'x'})))
    def test_verdict_websocket_event_does_not_inflate_counts(self):
        body={'domain':'one.example','label':'tracker','confidence':.9,'reason':'Usage telemetry','ts':1000}
        with TestClient(api.app) as client, client.websocket_connect('/ws/events') as ws:
            snapshot=ws.receive_json()
            self.assertEqual(client.post('/ai/verdict',json=body).json(),{'ok':True})
            self.assertEqual(ws.receive_json(),{'type':'ai_verdict',**body})
            self.assertEqual(client.get('/summary').json()['seen'],snapshot['totals']['seen'])
            for invalid in ({**body,'label':'bad'},{**body,'confidence':True},{**body,'reason':'x\n'}):
                self.assertEqual(client.post('/ai/verdict',json=invalid).status_code,422)
