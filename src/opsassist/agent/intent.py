"""Two checks on the router's decision, decided in code from the form of the message.

The router model reads meaning; these read shape, and each only ever moves a decision to a
path that cannot do more than the one it replaces:

* ``asks_about_rules``: a question about how things work, not addressed to the assistant.
  A "refuse" for it becomes "knowledge", which has no tools: at worst it abstains.
* ``asks_for_unsupported_action``: an instruction whose verb changes a system. A read-only
  skill chosen for it becomes "refuse": the user asked for something no skill does, and a
  status read would look like an answer while ignoring the request.

Neither holds example requests; they are general English forms and an operations
vocabulary, so they cannot memorise a graded case (D-34).
"""

from __future__ import annotations

import re


def _vocabulary(text: str) -> frozenset[str]:
    return frozenset(text.split())


_QUESTION_WORDS = _vocabulary(
    """can could may might must should shall will would do does did is are was were has have
    who whom whose what when where why how which"""
)
# Words that turn a question into a request to the assistant ("can you ...", "please ...").
_ADDRESSED = _vocabulary("you your yourself please")

# Leading words that soften an instruction without changing it.
_SOFTENERS = _vocabulary("please kindly now just go ahead and then")

# Verbs that change the state of a system. None of the read-only skills performs them.
_ACTIONS = _vocabulary(
    """restart reboot start stop shut shutdown kill terminate halt pause resume delete remove
    drop destroy wipe purge flush reset deploy redeploy release roll rollback revert scale resize
    patch upgrade downgrade update install uninstall migrate run execute restore drain
    disable enable block unblock grant revoke rotate change modify edit apply provision
    decommission failover promote demote"""
)


def _words(message: str) -> list[str]:
    return re.findall(r"[a-z]+", message.lower())


def asks_about_rules(message: str) -> bool:
    """A question (question word first, or a question mark) that does not ask the assistant
    to do anything: no "you", no "please", nothing "for me"."""
    words = _words(message)
    if not words:
        return False
    question = words[0] in _QUESTION_WORDS or message.rstrip().endswith("?")
    addressed = bool(_ADDRESSED & set(words)) or "for me" in " ".join(words)
    return question and not addressed


def asks_for_unsupported_action(message: str) -> bool:
    """An instruction whose first real word is a verb that changes a system."""
    words = [w for w in _words(message) if w not in _SOFTENERS]
    return bool(words) and words[0] in _ACTIONS
