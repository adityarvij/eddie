"""Classify the scripted feed with real local Ollama, using an in-memory queue.

This adapter is for the fake-traffic demo. Real DNS uses eddie.classifier and
its existing durable query-log queue. No demo audio or network history is saved.
"""
import json
import logging
import os
import sqlite3
import time
import urllib.request
from . import classifier, store

log = logging.getLogger('eddie.feed_classifier')

class FeedClassifier:
    def __init__(self, ask, publish):
        self.conn = sqlite3.connect(':memory:')
        self.conn.executescript(store.SCHEMA)
        self.worker = classifier.AsyncClassifier(self.conn, ask, publish)
        self.last_id = 0

    def ingest(self, events):
        newest = max((event.get('id',0) for event in events),default=0)
        if newest and newest < self.last_id:
            # API restart: ids start over. Existing inference/cache remains
            # valid by domain; reset only the source-event deduplication cursor.
            self.last_id = 0
        added = 0
        with self.conn:
            for event in events:
                if event.get('type') != 'query' or event.get('id',0) <= self.last_id:
                    continue
                self.conn.execute('INSERT INTO queries(ts,device_mac,domain,qtype,action,source,reason) VALUES(?,?,?,?,?,?,?)',
                                  tuple(event.get(key,'') for key in store.QUERY_COLUMNS))
                self.conn.execute('INSERT OR REPLACE INTO devices(mac,label) VALUES(?,?)',
                                  (event['device_mac'],event['device']))
                added += 1
            self.last_id = max(self.last_id,newest)
            self.conn.execute('DELETE FROM queries WHERE id NOT IN (SELECT id FROM queries ORDER BY id DESC LIMIT 20000)')
            cutoff = int(time.time()*1000)-classifier.LOOKBACK_MS
            self.conn.execute('DELETE FROM queries WHERE ts < ?', (cutoff,))
            self.conn.execute('DELETE FROM ai_verdicts WHERE ts < ?', (cutoff,))
        return added

    def close(self):
        self.worker.close()
        self.conn.close()


def fetch_events(base):
    classifier.check_local(base)
    with classifier._OPENER.open(base.rstrip('/')+'/events?limit=2000', timeout=2) as response:
        return json.load(response)


def main():
    logging.basicConfig(level=logging.INFO,format='%(name)s %(message)s')
    base = os.environ.get('EDDIE_API_URL','http://127.0.0.1:8000')
    url = os.environ.get('EDDIE_OLLAMA_URL',classifier.DEFAULT_URL)
    classifier.check_local(base); classifier.check_local(url)
    model = os.environ.get('EDDIE_CLASSIFIER_MODEL',classifier.DEFAULT_MODEL)
    adapter = FeedClassifier(lambda facts:classifier.ask_ollama(facts,url,model), classifier.publish_verdict)
    next_poll = 0
    log.info('real local AI enabled for the scripted demo feed; queue/cache are memory only')
    try:
        while True:
            if time.monotonic() >= next_poll:
                try:
                    adapter.ingest(fetch_events(base))
                except (OSError,ValueError,KeyError):
                    log.warning('feed unavailable; retrying')
                next_poll = time.monotonic()+1
            adapter.worker.tick()
            time.sleep(.1)
    except KeyboardInterrupt:
        pass
    finally:
        adapter.close()

if __name__=='__main__': main()
