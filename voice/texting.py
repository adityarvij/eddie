"""Voice texts (#37 follow-up): contacts, parsing, and the macOS Messages sender.

Message text and addresses live only in memory for the confirmation slot and the send.
Nothing here logs, prints or stores them.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

CONTACTS = Path(__file__).with_name('contacts.json')
MAX_BODY = 160  # one SMS; also keeps the word-for-word read-back short enough to answer in time
ADDRESS = re.compile(r'\+\d{7,15}|[^@\s]+@[^@\s]+\.[^@\s]+')
SERVICES = ('iMessage', 'SMS')
VERB = re.compile(r'\s*(?:(?:hey\s+)?eddie[\s,.!]+|ok(?:ay)?[\s,.!]+)*(?:please\s+)?(?:(?:can|could|would) you\s+)?'
                  r'(?:text|message|send (?:a )?(?:text|message) to)\s+(?P<rest>.*)', re.I | re.S)


class ContactsError(ValueError):
    pass


def load_contacts(path=None):
    """{spoken name: {'name', 'address', 'service'}} from EDDIE_CONTACTS or voice/contacts.json.

    Values are an E.164 number or email, or {"address": ..., "service": "iMessage" | "SMS"}.
    A missing file means no contacts; an invalid one raises ContactsError.
    """
    path = Path(path or os.environ.get('EDDIE_CONTACTS') or CONTACTS)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ContactsError(f'{path.name} could not be read') from error
    if not isinstance(raw, dict):
        raise ContactsError(f'{path.name} must map names to addresses')
    contacts = {}
    for name, value in raw.items():
        entry = {'address': value} if isinstance(value, str) else value
        address, service = (entry.get('address'), entry.get('service', 'iMessage')) if isinstance(entry, dict) else (None, None)
        if not isinstance(address, str) or not ADDRESS.fullmatch(address) or service not in SERVICES or not name.strip():
            raise ContactsError(f'{path.name} has an invalid entry for {name!r}')
        contacts[' '.join(name.lower().split())] = {'name': name.strip(), 'address': address, 'service': service}
    return contacts


def parse(raw, contacts):
    """(contact or None, body) for a whole-utterance text command, else None.

    The body is the rest of the transcript word for word, after the contact's name and any
    separating comma, colon or "saying". The longest matching contact name wins.
    """
    match = VERB.fullmatch(raw)
    if not match:
        return None
    rest = match['rest']
    for key in sorted(contacts, key=lambda k: -len(k)):
        name = re.match(r'[\W_]*'.join(map(re.escape, key.split())) + r'(?![\w])', rest, re.I)
        if name:
            body = re.sub(r'^[\s,:;.\-–—]*(?:saying\b[\s,:]*)?', '', rest[name.end():], flags=re.I)
            return contacts[key], body.strip()
    return None, ''


class MessagesSender:
    """Sends through macOS Messages (iMessage, or SMS via the iPhone's Text Message Forwarding).

    send() returns 'sent' only once Messages' own database shows the message sent without an
    error. Reading it needs Full Disk Access for the process running Eddie; without it the
    result is 'unknown', never 'sent'.
    """
    SCRIPT = '''on run argv
    set {theAddress, theBody, theService} to argv
    tell application "Messages"
        if theService is "SMS" then
            set theAccount to 1st account whose service type = SMS
        else
            set theAccount to 1st account whose service type = iMessage
        end if
        send theBody to participant theAddress of theAccount
    end tell
end run'''
    DATABASE = Path.home() / 'Library' / 'Messages' / 'chat.db'
    APPLE_EPOCH = 978307200  # 2001-01-01 UTC; chat.db dates are nanoseconds since then
    # The newest message to this address since the send started. Only status columns are
    # read, never the text.
    STATUS = ('SELECT m.is_sent, m.error FROM message m JOIN handle h ON m.handle_id = h.ROWID '
              'WHERE m.is_from_me = 1 AND m.date >= ? AND lower(h.id) = lower(?) ORDER BY m.date DESC LIMIT 1')

    def __init__(self, run=subprocess.run, connect=sqlite3.connect, clock=time.time, sleep=time.sleep,
                 ack_seconds=10, poll_seconds=0.5):
        self.run, self.connect, self.clock, self.sleep = run, connect, clock, sleep
        self.ack_seconds, self.poll_seconds = ack_seconds, poll_seconds

    def send(self, address, service, body):
        """'sent', 'failed' (Messages refused or reported an error), 'pending' (handed off,
        not confirmed in time) or 'unknown' (handed off, status unreadable)."""
        since = int((self.clock() - self.APPLE_EPOCH - 1) * 1e9)
        try:
            # Arguments, not string interpolation, so the text can't change the script.
            # Output is discarded: an AppleScript error can quote the message.
            result = self.run(['/usr/bin/osascript', '-e', self.SCRIPT, address, body, service],
                              capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            return 'pending'
        if result.returncode != 0:
            return 'failed'
        deadline = self.clock() + self.ack_seconds
        while True:
            try:
                with closing(self.connect(f'file:{self.DATABASE}?mode=ro', uri=True, timeout=1)) as db:
                    row = db.execute(self.STATUS, (since, address)).fetchone()
            except sqlite3.Error:
                return 'unknown'
            if row and row[1]:
                return 'failed'
            if row and row[0]:
                return 'sent'
            if self.clock() >= deadline:
                return 'pending'
            self.sleep(self.poll_seconds)


def configured_sender():
    """The sender EDDIE_TEXTING names, or None. Off unless set; Messages is macOS-only."""
    choice = os.environ.get('EDDIE_TEXTING', 'off').lower()
    if choice == 'messages' and sys.platform == 'darwin':
        return MessagesSender()
    return None
