"""Voice texts: word-for-word read-back, send only on a timely yes, "Sent" only on acknowledgment."""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'voice'))
import texting
import voice_agent

CONTACTS = {'Will': '+15551234567', 'Will Smith': {'address': 'will@example.com'},
            'Sam': {'address': '+15557654321', 'service': 'SMS'}}
BODY = "I'm running late."
READ_BACK = 'Text Will: "I\'m running late." Send it? Say yes to confirm or no to cancel.'


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


def write_contacts(test, data):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    path = Path(folder.name) / 'contacts.json'
    path.write_text(json.dumps(data))
    return path


class Parsing(unittest.TestCase):
    def setUp(self):
        self.contacts = texting.load_contacts(write_contacts(self, CONTACTS))

    def test_body_is_the_rest_of_the_transcript_word_for_word(self):
        for raw, name, body in [
            ("Text Will, I'm running late.", 'Will', BODY),
            ("Hey Eddie, text Will I'm running late.", 'Will', BODY),
            ('Could you message sam: Back by 6, save me some pizza!', 'Sam', 'Back by 6, save me some pizza!'),
            ('Send a text to Will Smith saying on my way', 'Will Smith', 'on my way'),
            ('text will smith', 'Will Smith', ''),
        ]:
            with self.subTest(raw=raw):
                contact, said = texting.parse(raw, self.contacts)
                self.assertEqual((contact['name'], said), (name, body))

    def test_unknown_contact_and_other_commands(self):
        self.assertEqual(texting.parse('text Bob hello', self.contacts), (None, ''))
        self.assertEqual(texting.parse('text Willow hello', self.contacts), (None, ''))
        for raw in ("What's the TV doing?", 'block the text', 'yes'):
            self.assertIsNone(texting.parse(raw, self.contacts))

    def test_contacts_file(self):
        self.assertEqual(self.contacts['sam'], {'name': 'Sam', 'address': '+15557654321', 'service': 'SMS'})
        self.assertEqual(self.contacts['will']['service'], 'iMessage')
        self.assertEqual(texting.load_contacts(Path(tempfile.gettempdir()) / 'no-such-contacts.json'), {})
        for bad in ({'Will': '555-1234'}, {'Will': {'address': '+15551234567', 'service': 'Fax'}}, ['Will']):
            with self.subTest(bad=bad), self.assertRaises(texting.ContactsError):
                texting.load_contacts(write_contacts(self, bad))


class TextFlow(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sender = Mock()
        self.sender.send.return_value = 'sent'
        patches = [
            patch.dict(os.environ, {'EDDIE_CONTACTS': str(write_contacts(self, CONTACTS))}),
            patch.object(voice_agent, 'text_sender', self.sender),
            patch.object(voice_agent, 'confirmation', voice_agent.Confirmation(clock=self.clock)),
            patch.object(voice_agent, 'ask_model', None),
            patch('voice_agent.request'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def ask(self):
        self.assertEqual(voice_agent.respond("Text Will, I'm running late."), READ_BACK)
        self.sender.send.assert_not_called()

    def test_yes_sends_the_read_back_text_once(self):
        self.ask()
        self.assertEqual(voice_agent.respond('Yes.'), 'Sent to Will.')
        self.sender.send.assert_called_once_with('+15551234567', 'iMessage', BODY)
        voice_agent.respond('yes')
        self.sender.send.assert_called_once()

    def test_no_sends_nothing(self):
        self.ask()
        self.assertEqual(voice_agent.respond('No.'), 'Cancelled. Nothing changed.')
        voice_agent.respond('yes')
        self.sender.send.assert_not_called()

    def test_unclear_reply_never_sends(self):
        self.ask()
        self.assertTrue(voice_agent.respond('yes but change it').startswith('I need a yes or no. Text Will: "'))
        self.assertEqual(voice_agent.respond('maybe'), "I didn't get a clear yes, so I didn't do it.")
        self.sender.send.assert_not_called()

    def test_timeout_sends_nothing(self):
        self.ask()
        self.clock.now += 20  # past the usual 15 s, inside the text's 30 s
        self.assertIsNotNone(voice_agent.confirmation.status())
        self.clock.now += 11
        self.assertEqual(voice_agent.respond('yes'),
                         "That request expired, so I didn't do it. Ask again if you still want it.")
        self.sender.send.assert_not_called()

    def test_sent_is_spoken_only_on_acknowledgment(self):
        for status, reply in [('failed', "Messages couldn't send that to Will."),
                              ('pending', "I handed that to Messages, but it hasn't confirmed"),
                              ('unknown', "I can't read its status on this Mac"),
                              (OSError('boom'), "I handed that to Messages, but it hasn't confirmed")]:
            with self.subTest(status=status):
                self.sender.send.reset_mock(return_value=True, side_effect=True)
                if isinstance(status, Exception):
                    self.sender.send.side_effect = status
                else:
                    self.sender.send.return_value = status
                self.ask()
                spoken = voice_agent.respond('yes')
                self.assertIn(reply, spoken)
                self.assertNotIn('Sent', spoken)
                self.sender.send.assert_called_once()

    def test_refusals_leave_nothing_pending(self):
        for raw, reply in [('text Bob hello', 'I can only text Will, Will Smith or Sam. Nothing was sent.'),
                           ('text Will', 'What should the text to Will say? Say text Will, then the message.'),
                           ('text Will ' + 'x' * 161, "That's too long for me to read back.")]:
            with self.subTest(raw=raw):
                self.assertIn(reply, voice_agent.respond(raw))
                self.assertIsNone(voice_agent.confirmation.status())
        with patch.object(voice_agent, 'text_sender', None):
            self.assertEqual(voice_agent.respond(f'text Will {BODY}'), "Texting isn't set up on this Eddie. Nothing was sent.")
        self.assertIsNone(voice_agent.confirmation.status())
        self.sender.send.assert_not_called()

    def test_screen_status_and_logs_carry_no_address(self):
        out = io.StringIO()
        with self.assertNoLogs(level='DEBUG'), redirect_stdout(out):
            self.ask()
            status = voice_agent.confirmation.status()
            voice_agent.respond('yes')
        self.assertEqual(set(status), {'kind', 'prompt', 'expires_in_ms'})
        self.assertEqual(status['kind'], 'text')
        self.assertNotIn('5551234567', json.dumps(status))
        self.assertEqual(out.getvalue(), '')


class MessagesSenderAck(unittest.TestCase):
    """The real SQL against a stand-in chat.db; osascript is faked."""
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / 'chat.db'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript('CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);'
                             'CREATE TABLE message (ROWID INTEGER PRIMARY KEY, handle_id INTEGER, is_from_me INTEGER,'
                             ' date INTEGER, is_sent INTEGER, error INTEGER, text TEXT);'
                             "INSERT INTO handle VALUES (1, '+15551234567');")
        patcher = patch.object(texting.MessagesSender, 'DATABASE', self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 1_800_000_000.0
        self.run = Mock(return_value=subprocess.CompletedProcess([], 0))

    def sender(self, **kwargs):
        def sleep(seconds): self.now += seconds
        return texting.MessagesSender(run=self.run, clock=lambda: self.now, sleep=sleep, **kwargs)

    def message(self, is_sent, error=0, seconds_ago=0):
        date = int((self.now - texting.MessagesSender.APPLE_EPOCH - seconds_ago) * 1e9)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO message (handle_id, is_from_me, date, is_sent, error, text) VALUES (1, 1, ?, ?, ?, ?)',
                       (date, is_sent, error, BODY))

    def test_text_is_an_argument_not_part_of_the_script(self):
        self.message(1)
        self.assertEqual(self.sender().send('+15551234567', 'iMessage', BODY), 'sent')
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[:2], ['/usr/bin/osascript', '-e'])
        self.assertNotIn(BODY, argv[2])
        self.assertEqual(argv[3:], ['+15551234567', BODY, 'iMessage'])
        self.assertNotIn('text', texting.MessagesSender.STATUS.split('FROM')[0])

    def test_error_row_is_a_failure(self):
        self.message(0, error=22)
        self.assertEqual(self.sender().send('+15551234567', 'SMS', BODY), 'failed')

    def test_unsent_until_deadline_is_pending(self):
        self.message(1, seconds_ago=60)  # an older sent message to the same person doesn't count
        self.message(0)
        self.assertEqual(self.sender(ack_seconds=2).send('+15551234567', 'iMessage', BODY), 'pending')

    def test_osascript_failure_or_timeout(self):
        self.run.return_value = subprocess.CompletedProcess([], 1)
        self.assertEqual(self.sender().send('+15551234567', 'iMessage', BODY), 'failed')
        self.run.side_effect = subprocess.TimeoutExpired('osascript', 15)
        self.assertEqual(self.sender().send('+15551234567', 'iMessage', BODY), 'pending')

    def test_unreadable_database_is_unknown_not_sent(self):
        self.db.unlink()
        self.assertEqual(self.sender().send('+15551234567', 'iMessage', BODY), 'unknown')

    def test_off_unless_configured_on_macos(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(texting.configured_sender())
        with patch.dict(os.environ, {'EDDIE_TEXTING': 'messages'}), patch.object(texting.sys, 'platform', 'linux'):
            self.assertIsNone(texting.configured_sender())
        with patch.dict(os.environ, {'EDDIE_TEXTING': 'messages'}), patch.object(texting.sys, 'platform', 'darwin'):
            self.assertIsInstance(texting.configured_sender(), texting.MessagesSender)


if __name__ == '__main__':
    unittest.main()
