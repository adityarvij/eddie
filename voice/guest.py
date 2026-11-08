"""Guest Mode permissions (#163): which of Eddie's tools a guest may use. Deny by default.

Eddie can't tell voices apart, so while Guest Mode is on every voice and typed ask is
treated as a guest's. The owner turns Guest Mode on and off, and picks what guests may
use, from the dashboard on the Eddie computer (POST /api/guest), never by voice.

The profile is a local JSON file (owner-only, mode 0600): {"active": bool, "allowed": [...]}.
Only APPROVABLE categories can be allowed. OWNER_ONLY ones are refused whatever the file
says, and an unreadable file turns Guest Mode on with nothing allowed (fail closed).
"""
import json
import os
import threading
from pathlib import Path

# Owner-only: never allowed for a guest, whatever the owner selects.
OWNER_ONLY = ('traffic', 'reports', 'rules', 'modes', 'devices', 'contacts', 'calendars',
              'cameras', 'locks', 'alarms')
# What the owner may approve for guests. The house guide (#165) holds Wi-Fi and the like, each entry
# marked guest or owner. tv, lights and thermostat have no tool yet (#166); the choice is stored so
# they're gated from the start.
APPROVABLE = ('house_guide', 'timers', 'tv', 'lights', 'thermostat')
# Always allowed: they share nothing. Turning the mic off only shares less.
ALWAYS = ('greeting', 'microphone_off')

OWNER_ONLY_REPLY = "That's only for the household, so I can't help with it in guest mode."
NOT_APPROVED_REPLY = "Your host hasn't turned that on for guests, so I can't help with it."
HELP = "I'm in guest mode, so I can only help with what your host has turned on."


class Profile:
    """The Guest Mode switch and the owner's approved categories. path None keeps it in memory."""
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.active = False
        self.allowed = set()
        self.load_error = None
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if type(data.get('active')) is not bool or not isinstance(data.get('allowed'), list):
                    raise ValueError('expected active and allowed')
                self.active = data['active']
                self.allowed = {c for c in data['allowed'] if c in APPROVABLE}
            except (OSError, ValueError, AttributeError) as error:
                self.active, self.allowed = True, set()
                self.load_error = (f'Guest profile {self.path} unreadable ({error}); guest mode is on with '
                                   'nothing allowed until the owner saves it from the dashboard.')

    def save(self, active, allowed):
        if not self.path:
            return
        tmp = self.path.with_name(self.path.name + '.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as file:
            json.dump({'active': active, 'allowed': sorted(allowed)}, file)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def update(self, active=None, allowed=None):
        """Owner change from the dashboard. Raises ValueError for anything but a bool and approvable names."""
        if active is not None and type(active) is not bool:
            raise ValueError('active must be true or false')
        if allowed is not None and (not isinstance(allowed, list) or not all(c in APPROVABLE for c in allowed)):
            raise ValueError(f"allowed must list only {', '.join(APPROVABLE)}")
        with self.lock:
            active = self.active if active is None else active
            allowed = self.allowed if allowed is None else set(allowed)
            self.save(active, allowed)  # an OSError leaves the profile as it was
            self.active, self.allowed, self.load_error = active, allowed, None
        return self.view()

    def view(self):
        with self.lock:
            return {'active': self.active, 'allowed': sorted(self.allowed),
                    'approvable': list(APPROVABLE), 'owner_only': list(OWNER_ONLY)}

    def role(self):
        """'guest' while Guest Mode is on (every speaker is unknown), else 'owner'."""
        return 'guest' if self.active else 'owner'

    def allows(self, category):
        if category in ALWAYS:
            return True
        with self.lock:
            if not self.active:
                return category in OWNER_ONLY or category in APPROVABLE
            return category in APPROVABLE and category in self.allowed

    def check(self, category):
        """None when the active role may use category, else the spoken refusal. It names no private data."""
        if self.allows(category):
            return None
        return NOT_APPROVED_REPLY if category in APPROVABLE else OWNER_ONLY_REPLY
