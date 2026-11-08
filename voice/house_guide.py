"""Local concierge house guide (#165): the owner's notes on Wi-Fi, checkout, towels, parking and so on.

Answers are the owner's own words, quoted, from one local JSON file read on each question.
Nothing here logs, prints or stores the guide or the questions, and nothing reaches a model.
"""
import json
import os
import re
from pathlib import Path

GUIDE = Path(__file__).with_name('house_guide.json')
AUDIENCES = ('guest', 'owner')
FIELDS = {'topic', 'answer', 'keywords', 'audience'}
DONT_KNOW = "I don't know that one; ask the host."
UNREADABLE = "I can't read the house guide; ask the host."
# Questions that sound like they're about the house, so Eddie owns up to not knowing
# instead of guessing, even with no guide or no matching entry.
HOUSE_TOPICS = re.compile(
    r'\b(?:wifi|wi fi|internet|checkout|check out|checkin|check in|towels?|linens?|sheets|blankets?|parking|park'
    r'|house rules|quiet hours|trash|garbage|recycling|bins?|thermostat|heating|air conditioning'
    r'|dishwasher|washing machine|washer|dryer|laundry|oven|stove|microwave|coffee maker|hot tub|pool'
    r'|lockbox|door code|spare key|keys|fireplace|grill|smoking|pets?)\b')
QUESTION = re.compile(
    r'(?:(?:ok|okay|eddie|hey eddie|please) )*(?:what|whats|where|wheres|when|whens|which|who|how|hows|is|are|do|does'
    r'|can (?:i|we|guests)|could (?:i|we)|may (?:i|we)|should (?:i|we)|tell me|any) .*')


class GuideError(ValueError):
    pass


def normal(text):
    """Lower case, letters and digits only, single spaces; "Wi-Fi" and "what's" become "wifi" and "whats"."""
    return ' '.join(re.sub(r'[^a-z0-9\s]', ' ', re.sub(r"['’-]", '', text.lower())).split())


def load(path=None):
    """[{'topic', 'answer', 'keywords', 'audience'}] from EDDIE_HOUSE_GUIDE or voice/house_guide.json.

    The file is {"entries": [...]}; each entry has a topic and an answer, optional keywords,
    and an audience of "guest" or "owner". Entries are owner-only unless marked "guest".
    A missing file means an empty guide; an invalid one raises GuideError.
    """
    path = Path(path or os.environ.get('EDDIE_HOUSE_GUIDE') or GUIDE)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise GuideError(f'{path.name} could not be read') from error
    if not isinstance(raw, dict) or not isinstance(raw.get('entries'), list):
        raise GuideError(f'{path.name} must be {{"entries": [...]}}')
    entries = []
    for number, entry in enumerate(raw['entries'], 1):
        if not isinstance(entry, dict) or set(entry) - FIELDS:
            raise GuideError(f'{path.name} entry {number} has unknown fields')
        topic, answer = entry.get('topic'), entry.get('answer')
        keywords, audience = entry.get('keywords', []), entry.get('audience', 'owner')
        if (not isinstance(topic, str) or not normal(topic) or not isinstance(answer, str) or not answer.strip()
                or not isinstance(keywords, list) or not all(isinstance(k, str) and normal(k) for k in keywords)
                or audience not in AUDIENCES):
            raise GuideError(f'{path.name} entry {number} is invalid')
        entries.append({'topic': topic.strip(), 'answer': answer.strip(), 'audience': audience,
                        'keywords': sorted({normal(topic)} | {normal(k) for k in keywords})})
    return entries


def mentions(text, phrase):
    """True if the normalised text contains the phrase as whole words, allowing a plural s."""
    return re.search(rf'\b{re.escape(phrase)}s?\b', text) is not None


def score(entry, text):
    """Words of the entry's keywords that the text mentions; "checkout time" outranks "time"."""
    return sum(len(k.split()) for k in entry['keywords'] if mentions(text, k))


def visible(entries, role):
    """What this role may hear: guests only guest entries, the owner all of them. Unknown roles get guest."""
    return entries if role == 'owner' else [e for e in entries if e['audience'] == 'guest']


def answer(text, role='guest', entries=None, path=None):
    """The spoken reply to a house question, or None if it isn't one.

    It is one if it's phrased as a question (or is just a topic's name, the reply to "which one?")
    and names a guide keyword or a common house topic. One best match is quoted word for word;
    a tie asks which; no match, or a match only the owner may hear, is an honest don't-know.
    """
    text = normal(text)
    try:
        entries = load(path) if entries is None else entries
    except GuideError:
        return UNREADABLE if QUESTION.fullmatch(text) and HOUSE_TOPICS.search(text) else None
    named = any(re.fullmatch(rf'(?:the )?{re.escape(k)}s?(?: please)?', text) for e in entries for k in e['keywords'])
    asked = QUESTION.fullmatch(text) is not None
    if not named and not (asked and (HOUSE_TOPICS.search(text) or any(score(e, text) for e in entries))):
        return None
    scored = [(score(e, text), e) for e in visible(entries, role)]
    best = max((s for s, _ in scored), default=0)
    if not best:
        return DONT_KNOW
    top = [e for s, e in scored if s == best]
    if len(top) == 1:
        return top[0]['answer']
    topics = [e['topic'] for e in top]
    return f"I have notes on {', '.join(topics[:-1])} and {topics[-1]}. Which one?"
