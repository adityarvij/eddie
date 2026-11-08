"""Voice timers and reminders (#122): parsing, scheduling, persistence, list and cancel."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import timers
import voice_agent

SAT = datetime(2026, 11, 7, 15, 30)  # a Saturday afternoon


class Clock:
    def __init__(self, now=SAT): self.now = now.timestamp()
    def __call__(self): return self.now


def store(path=None):
    return timers.Timers(path, clock=Clock())


def due(store_):
    return [datetime.fromtimestamp(item['due']) for item in store_.pending()]


class TimerParsingTests(unittest.TestCase):
    def test_durations(self):
        for text, seconds in [('set a timer for 10 minutes', 600), ('Set a 10-minute timer.', 600),
                              ('timer for an hour and a half', 5400), ('set a timer for half an hour', 1800),
                              ('start a timer for 1 hour and 30 minutes', 5400), ('set a timer for ninety seconds', 90),
                              ('set a timer for twenty five minutes', 1500), ('can you set a timer for 90 seconds please', 90),
                              ('set a timer for a minute and a half', 90), ('set a timer for 2 hours 15 minutes', 8100)]:
            s = store()
            reply = timers.handle(s, text)
            self.assertTrue(reply.startswith('Timer set for'), (text, reply))
            self.assertEqual(s.pending()[0]['seconds'], seconds, text)
            self.assertEqual(due(s), [SAT + timedelta(seconds=seconds)], text)

    def test_spoken_back(self):
        self.assertEqual(timers.handle(store(), 'set a timer for 1 hour and 30 minutes'), 'Timer set for 1 hour and 30 minutes.')

    def test_too_long_or_zero_sets_nothing(self):
        s = store()
        self.assertEqual(timers.handle(s, 'set a timer for 25 hours'), 'Timers can only be 24 hours.')
        self.assertEqual(timers.handle(s, 'set a timer for 0 minutes'), 'Timers can only be 24 hours.')
        self.assertEqual(s.pending(), [])

    def test_not_a_timer_command(self):
        for text in ("what's been going on", 'block it', 'the timer is broken', 'yes', 'cancel'):
            self.assertIsNone(timers.handle(store(), text), text)


class ReminderParsingTests(unittest.TestCase):
    def remind(self, text, now=SAT):
        s = timers.Timers(clock=Clock(now))
        return timers.handle(s, text), s

    def test_relative(self):
        reply, s = self.remind('remind me in 20 minutes to check the oven')
        self.assertEqual(reply, "I'll remind you in 20 minutes to check the oven.")
        self.assertEqual(due(s), [SAT + timedelta(minutes=20)])
        self.assertEqual(s.pending()[0]['task'], 'check the oven')

    def test_task_first(self):
        reply, s = self.remind('Remind me to call my mom at 6 p.m.')
        self.assertEqual(reply, "I'll remind you at 6 PM to call your mom.")
        self.assertEqual(due(s), [datetime(2026, 11, 7, 18, 0)])

    def test_bare_hour_is_the_next_one(self):
        self.assertEqual(due(self.remind('remind me at 6 to take out the trash')[1]), [datetime(2026, 11, 7, 18, 0)])
        # At 7 PM, "at 6" is 6 AM tomorrow.
        reply, s = self.remind('remind me at 6 to take out the trash', SAT.replace(hour=19))
        self.assertEqual(due(s), [datetime(2026, 11, 8, 6, 0)])
        self.assertEqual(reply, "I'll remind you at 6 AM tomorrow to take out the trash.")

    def test_clock_forms(self):
        for text, when in [('remind me at 6:30 pm to eat', datetime(2026, 11, 7, 18, 30)),
                           ('remind me at six thirty to eat', datetime(2026, 11, 7, 18, 30)),
                           ('remind me at 18:45 to eat', datetime(2026, 11, 7, 18, 45)),
                           ('remind me at 7 oclock to eat', datetime(2026, 11, 7, 19, 0)),
                           ('remind me at noon tomorrow to eat', datetime(2026, 11, 8, 12, 0)),
                           ('remind me at midnight to sleep', datetime(2026, 11, 8, 0, 0)),
                           ('remind me tomorrow at 7 to run', datetime(2026, 11, 8, 7, 0)),
                           ('remind me tomorrow at 3 to call', datetime(2026, 11, 8, 15, 0)),
                           ('remind me at 9 tonight to lock up', datetime(2026, 11, 7, 21, 0)),
                           ('set a reminder for 5 pm to leave', datetime(2026, 11, 7, 17, 0)),
                           ('remind me to stretch in an hour', SAT + timedelta(hours=1))]:
            self.assertEqual(due(self.remind(text)[1]), [when], text)

    def test_task_containing_a_time_keeps_it(self):
        reply, s = self.remind('remind me to catch the 5 pm train at 4 pm')
        self.assertEqual(s.pending()[0]['task'], 'catch the 5 pm train')
        self.assertEqual(due(s), [datetime(2026, 11, 7, 16, 0)])

    def test_past_time_today_sets_nothing(self):
        reply, s = self.remind('remind me at 2 pm today to call')
        self.assertEqual(reply, 'That time already passed today.')
        self.assertEqual(s.pending(), [])

    def test_no_time_asks_when(self):
        for text in ('remind me to call mom', 'remind me tomorrow to call mom', 'set a reminder'):
            reply, s = self.remind(text)
            self.assertEqual(reply, timers.NOT_WHEN, text)
            self.assertEqual(s.pending(), [])

    def test_no_task_is_fine(self):
        reply, s = self.remind('remind me in 5 minutes')
        self.assertEqual(reply, "I'll remind you in 5 minutes.")
        self.assertIsNone(s.pending()[0]['task'])

    def test_more_than_a_week_ahead_refused(self):
        self.assertEqual(self.remind('remind me in 200 hours to renew')[0],
                         'Reminders can only be 7 days ahead.')


class ListCancelTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = timers.Timers(clock=self.clock)

    def say(self, text):
        return timers.handle(self.store, text)

    def test_list(self):
        self.assertEqual(self.say('what timers do I have'), "You don't have any timers or reminders.")
        self.say('set a timer for 10 minutes')
        self.clock.now += 330
        self.assertEqual(self.say("how much time is left on my timer?"),
                         'You have a timer for 10 minutes with 4 minutes and 30 seconds left.')
        self.say('remind me at 6 pm to call mom')
        self.assertEqual(self.say('list my reminders'),
                         'You have 2: a timer for 10 minutes with 4 minutes and 30 seconds left and '
                         'a reminder at 6 PM to call mom.')

    def test_cancel_the_only_one(self):
        self.say('set a timer for 10 minutes')
        self.assertEqual(self.say('cancel the timer'), 'Cancelled a timer for 10 minutes with 10 minutes left.')
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.say('cancel the timer'), "You don't have any timers.")

    def test_cancel_asks_which_when_several(self):
        self.say('set a timer for 10 minutes')
        self.say('set a timer for 1 hour')
        reply = self.say('cancel my timer')
        self.assertTrue(reply.endswith('Which one?'), reply)
        self.assertEqual(len(self.store.pending()), 2)
        self.assertEqual(self.say('cancel the 10 minute timer'), 'Cancelled a timer for 10 minutes with 10 minutes left.')
        self.assertEqual([i['seconds'] for i in self.store.pending()], [3600])

    def test_cancel_by_duration_and_task(self):
        self.say('set a timer for 10 minutes')
        self.say('remind me at 6 pm to call my mom')
        self.say('remind me at 7 pm to feed the cat')
        self.assertEqual(self.say('cancel the timer for 5 minutes'), "I couldn't find that timer.")
        self.assertEqual(self.say('cancel the reminder to call mom'), 'Cancelled a reminder at 6 PM to call your mom.')
        self.assertEqual([i['task'] for i in self.store.pending() if i['kind'] == 'reminder'], ['feed the cat'])

    def test_cancel_all(self):
        self.say('set a timer for 10 minutes')
        self.say('set a timer for 20 minutes')
        self.say('remind me in an hour to stretch')
        self.assertEqual(self.say('cancel all timers'), 'Cancelled 2 timers.')
        self.assertEqual([i['kind'] for i in self.store.pending()], ['reminder'])
        self.say('set a timer for 10 minutes')
        self.assertEqual(self.say('cancel all timers and reminders'), 'Cancelled 2 timers and reminders.')
        self.assertEqual(self.store.pending(), [])

    def test_limit(self):
        for _ in range(timers.MAX_ITEMS):
            self.say('set a timer for 10 minutes')
        self.assertIn("cancel one first", self.say('set a timer for 10 minutes'))
        self.assertEqual(len(self.store.pending()), timers.MAX_ITEMS)


class SchedulingTests(unittest.TestCase):
    def test_take_due_rings_each_once_in_order(self):
        clock = Clock()
        s = timers.Timers(clock=clock)
        timers.handle(s, 'set a timer for 2 minutes')
        timers.handle(s, 'remind me in 1 minute to stir')
        self.assertEqual(s.take_due(), [])
        clock.now += 60
        rang = s.take_due()
        self.assertEqual([i['kind'] for i in rang], ['reminder'])
        self.assertEqual(s.take_due(), [])
        clock.now += 61
        self.assertEqual([i['kind'] for i in s.take_due()], ['timer'])
        self.assertEqual(s.pending(), [])

    def test_announcements(self):
        timer = {'kind': 'timer', 'due': SAT.timestamp(), 'seconds': 600, 'task': None}
        self.assertEqual(timers.announcement(timer, SAT), ('Timer done', 'Your timer for 10 minutes is done.'))
        self.assertEqual(timers.announcement(timer, SAT + timedelta(minutes=5)),
                         ('Timer done', "Your timer for 10 minutes finished at 3:30 PM."))
        reminder = {'kind': 'reminder', 'due': SAT.timestamp(), 'seconds': None, 'task': 'call my mom'}
        self.assertEqual(timers.announcement(reminder, SAT), ('Reminder', 'Reminder to call your mom.'))
        self.assertEqual(timers.announcement(dict(reminder, task=None), SAT), ('Reminder', 'This is your reminder.'))
        self.assertEqual(timers.announcement(reminder, SAT + timedelta(hours=2)),
                         ('Reminder', 'Your reminder for 3:30 PM to call your mom.'))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'timers.json'

    def tearDown(self):
        self.dir.cleanup()

    def test_survives_restart_and_overdue_rings_after_it(self):
        s = store(self.path)
        timers.handle(s, 'set a timer for 10 minutes')
        timers.handle(s, 'remind me at 6 pm to call mom')
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        clock = Clock(SAT + timedelta(minutes=30))  # restarted after the timer was due
        again = timers.Timers(self.path, clock=clock)
        self.assertEqual([i['kind'] for i in again.pending()], ['timer', 'reminder'])
        rang = again.take_due()
        self.assertEqual([i['kind'] for i in rang], ['timer'])
        self.assertIn("finished at", timers.announcement(rang[0], datetime.fromtimestamp(clock.now))[1])
        # Rung items are gone from disk too, so a second restart doesn't ring them again.
        self.assertEqual([i['kind'] for i in json.loads(self.path.read_text())], ['reminder'])

    def test_cancel_is_written(self):
        s = store(self.path)
        timers.handle(s, 'set a timer for 10 minutes')
        timers.handle(s, 'cancel the timer')
        self.assertEqual(json.loads(self.path.read_text()), [])

    def test_unreadable_file_starts_empty(self):
        self.path.write_text('{not json')
        s = store(self.path)
        self.assertEqual(s.pending(), [])
        self.assertIn('unreadable', s.load_error)

    def test_memory_only_writes_nothing(self):
        s = store()
        timers.handle(s, 'set a timer for 10 minutes')
        self.assertEqual(list(Path(self.dir.name).iterdir()), [])


class AlarmTests(unittest.TestCase):
    class FakeSD:
        def __init__(self): self.played = []
        def play(self, samples, rate): self.played.append((samples, rate))
        def wait(self): pass

    def test_default_chime_is_generated(self):
        sd = self.FakeSD()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('EDDIE_ALARM_SOUND', None)
            timers.play_alarm(sd)
        samples, rate = sd.played[0]
        self.assertEqual(rate, 22050)
        self.assertAlmostEqual(len(samples) / rate, 3.4, delta=.1)
        self.assertLessEqual(abs(samples).max(), .25)

    def test_custom_wav(self):
        import wave
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bell.wav'
            with wave.open(str(path), 'wb') as file:
                file.setnchannels(2)
                file.setsampwidth(2)
                file.setframerate(8000)
                file.writeframes((np.ones((8000 * 12, 2)) * 16384).astype('<i2').tobytes())
            sd = self.FakeSD()
            with patch.dict(os.environ, {'EDDIE_ALARM_SOUND': str(path)}):
                timers.play_alarm(sd)
            samples, rate = sd.played[0]
            self.assertEqual((rate, len(samples)), (8000, 8000 * timers.ALARM_SECONDS))  # mono, cut to 10 s
            self.assertAlmostEqual(float(samples[0]), .5)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.saved = voice_agent.timer_store
        voice_agent.timer_store = store()
        voice_agent.confirmation.cancel()

    def tearDown(self):
        voice_agent.timer_store = self.saved
        voice_agent.confirmation.cancel()

    def test_router_sets_lists_and_cancels_without_the_model_or_backend(self):
        with patch.object(voice_agent, 'ask_model', lambda text: self.fail('model called')), \
                patch.object(voice_agent, 'request', lambda *a, **k: self.fail('backend called')):
            self.assertEqual(voice_agent.respond('Hey Eddie, set a timer for 10 minutes.'), 'Timer set for 10 minutes.')
            self.assertIn('timer for 10 minutes', voice_agent.respond('What timers do I have?'))
            self.assertIn('Cancelled', voice_agent.respond('Cancel the timer.'))

    def test_bare_cancel_still_answers_a_pending_confirmation(self):
        voice_agent.confirmation.request('home_mode', {'enabled': True}, 'Turn on home mode?')
        self.assertEqual(voice_agent.respond('cancel'), 'Cancelled.')


if __name__ == '__main__':
    unittest.main()
