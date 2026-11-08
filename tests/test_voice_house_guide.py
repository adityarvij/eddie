"""Local concierge house guide (#165): schema, lookup, ambiguity, missing answers and owner-only entries."""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import guest
import house_guide
import voice_agent

EXAMPLE = Path(__file__).resolve().parents[1] / 'voice' / 'house_guide.example.json'
WIFI = 'The network is ExampleHouse-Guest and the password is on the card by the front door.'
CHECKOUT = 'Checkout is at 11 AM. Please start the dishwasher and leave the keys on the kitchen counter.'
SECRET = 'Example owner-only note: replace this with your own, or delete it. Guests never hear owner entries.'


class GuideFile:
    """A temporary guide that EDDIE_HOUSE_GUIDE points at."""
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
        handle.write(self.content if isinstance(self.content, str) else json.dumps(self.content))
        handle.close()
        self.path = handle.name
        self.env = patch.dict(os.environ, {'EDDIE_HOUSE_GUIDE': self.path})
        self.env.start()
        return self.path

    def __exit__(self, *exc):
        self.env.stop()
        os.unlink(self.path)


class SchemaTests(unittest.TestCase):
    def test_example_loads_and_entries_default_to_owner_only(self):
        entries = house_guide.load(EXAMPLE)
        self.assertEqual(len(entries), 8)
        with GuideFile({'entries': [{'topic': 'Gate code', 'answer': '1234'}]}):
            self.assertEqual(house_guide.load()[0]['audience'], 'owner')

    def test_missing_file_is_an_empty_guide(self):
        self.assertEqual(house_guide.load('/nonexistent/house_guide.json'), [])

    def test_invalid_files_raise(self):
        for content in ('not json', [], {'entries': {}}, {'entries': ['wifi']},
                        {'entries': [{'topic': 'Wi-Fi'}]}, {'entries': [{'topic': '', 'answer': 'x'}]},
                        {'entries': [{'topic': 'Wi-Fi', 'answer': 'x', 'audience': 'everyone'}]},
                        {'entries': [{'topic': 'Wi-Fi', 'answer': 'x', 'keywords': 'wifi'}]},
                        {'entries': [{'topic': 'Wi-Fi', 'answer': 'x', 'audiance': 'guest'}]}):
            with GuideFile(content), self.assertRaises(house_guide.GuideError, msg=content):
                house_guide.load()

    def test_starter_example_has_no_credentials_or_personal_data(self):
        text = EXAMPLE.read_text()
        self.assertIsNone(re.search(r'\+?\d{7,}|@|https?://', text))  # no phone numbers, emails or links
        self.assertIsNone(re.search(r'password is (?!on the card)', text, re.I))


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.entries = house_guide.load(EXAMPLE)

    def ask(self, text, role='guest'):
        return house_guide.answer(text, role, entries=self.entries)

    def test_answers_quote_the_configured_entry(self):
        for question, start in [("What's the Wi-Fi password?", WIFI), ('how do i get on the wifi', WIFI),
                                ('Hey Eddie, what time is checkout?', CHECKOUT), ('when do we need to leave', CHECKOUT),
                                ('Where are the towels?', 'Fresh towels'), ('can i park on the street', 'Park in'),
                                ('What are the house rules?', 'Quiet hours'), ('how do I start the dishwasher', 'Pods'),
                                ('where do i do laundry', 'Detergent')]:
            self.assertTrue(self.ask(question).startswith(start), (question, self.ask(question)))

    def test_more_specific_keywords_win(self):
        # "washing machine" (two words) outranks "dishwasher" (one); "washer" never matches inside "dishwasher"
        self.assertTrue(self.ask('is the washing machine next to the dishwasher').startswith('Detergent'))
        self.assertTrue(self.ask('how does the dish washer work').startswith('Pods'))

    def test_ambiguity_asks_which_and_the_topic_alone_answers(self):
        reply = self.ask('how do the dishwasher and the laundry work')
        self.assertEqual(reply, 'I have notes on dishwasher and washing machine. Which one?')
        self.assertTrue(self.ask('The dishwasher.').startswith('Pods'))

    def test_missing_answers_are_honest(self):
        for question in ('where is the hot tub', 'how does the thermostat work', 'is there a coffee maker'):
            self.assertEqual(self.ask(question), house_guide.DONT_KNOW, question)
        self.assertEqual(house_guide.answer('whats the wifi password', entries=[]), house_guide.DONT_KNOW)

    def test_not_house_questions_pass_through(self):
        for said in ('block doubleclick.net', 'turn on home mode', 'whats been going on', 'set a timer for 10 minutes',
                     'the alarm went off', 'turn off the wifi'):
            self.assertIsNone(self.ask(said), said)

    def test_owner_only_entries_never_reach_guests(self):
        for question in ('what is the alarm code', 'whats the security code', 'the alarm code'):
            self.assertEqual(self.ask(question), house_guide.DONT_KNOW, question)
            self.assertEqual(self.ask(question, role='owner'), SECRET, question)
        self.assertEqual(self.ask('what is the alarm code', role='kid'), house_guide.DONT_KNOW)  # unknown role: guest

    def test_ambiguity_never_names_owner_only_topics(self):
        entries = self.entries + [{'topic': 'Wi-Fi admin', 'answer': 'Router login is private.', 'audience': 'owner',
                                   'keywords': ['wifi']}]
        self.assertEqual(house_guide.answer('whats the wifi', 'guest', entries=entries), WIFI)
        self.assertIn('Which one?', house_guide.answer('whats the wifi', 'owner', entries=entries))

    def test_unreadable_guide_says_so_without_guessing(self):
        with GuideFile('{broken'):
            self.assertEqual(house_guide.answer('whats the wifi password'), house_guide.UNREADABLE)
            self.assertIsNone(house_guide.answer('turn on home mode'))


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.guide = GuideFile(EXAMPLE.read_text())
        self.guide.__enter__()
        self.profile = voice_agent.profile
        voice_agent.profile = guest.Profile()

    def tearDown(self):
        self.guide.__exit__()
        voice_agent.ask_model = None
        voice_agent.profile = self.profile

    def test_router_answers_from_the_guide_before_the_model(self):
        voice_agent.ask_model = lambda text: self.fail('the model must not see guide questions')
        self.assertEqual(voice_agent.respond("What's the Wi-Fi password?"), WIFI)
        self.assertEqual(voice_agent.respond('where is the hot tub'), house_guide.DONT_KNOW)

    def test_owner_entries_only_while_guest_mode_is_off(self):
        self.assertEqual(voice_agent.respond('what is the alarm code'), SECRET)
        voice_agent.profile.update(active=True, allowed=['house_guide'])
        self.assertEqual(voice_agent.respond('what is the alarm code'), house_guide.DONT_KNOW)
        self.assertEqual(voice_agent.respond("What's the Wi-Fi password?"), WIFI)

    def test_guests_need_the_guide_approved(self):
        voice_agent.profile.update(active=True)
        for said in ("What's the Wi-Fi password?", 'what is the alarm code', 'where is the hot tub'):
            self.assertEqual(voice_agent.respond(said), guest.NOT_APPROVED_REPLY, said)

    def test_traffic_questions_still_reach_the_backend(self):
        with patch('voice_agent.what_they_know', return_value={'text': 'From the backend.'}):
            self.assertEqual(voice_agent.respond('what does the router know about me'), 'From the backend.')
        with patch('voice_agent.request', return_value={'text': 'Summary.'}):
            self.assertEqual(voice_agent.respond('whats been going on'), 'Summary.')

    def test_guide_and_questions_are_not_printed(self):
        with patch('builtins.print') as printed, patch('logging.Logger._log') as logged:
            voice_agent.respond("What's the Wi-Fi password?")
            voice_agent.respond('what is the alarm code')
        printed.assert_not_called()
        logged.assert_not_called()


if __name__ == '__main__':
    unittest.main()
