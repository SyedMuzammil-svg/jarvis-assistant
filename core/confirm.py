"""
core/confirm.py — a confirmation the model cannot forge.

THE PROBLEM WITH THE OLD GATE
    computer_settings guarded shutdown and restart like this:

        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return "Please confirm by calling again with confirmed=yes."

    `confirmed` is a tool parameter, which means the *model* writes it. Nothing
    stops it from sending confirmed=yes on the first call, and nothing checks
    that a human was ever involved. It is a convention, not a gate — and its
    coverage was two actions, so deleting files and switching off the WiFi the
    assistant is talking over went through with no gate at all.

THE DESIGN HERE
    The confirmation token is issued by the *interface*, never by the model:

      1. An action calls `request(...)` with a callable that does the real work.
      2. This module hands the UI a banner with CONFIRM / CANCEL and returns
         IMMEDIATELY with a sentence for the model to say out loud.
      3. If — and only if — the user presses CONFIRM, the UI calls `resolve()`,
         which runs the stored callable off the Qt thread.

    Nothing blocks. The model keeps talking while the banner is up, so this
    costs no latency at all; in fact it is cheaper than the old gate, which
    burned two tool round trips (reject, then re-call) on every shutdown.

THE QUEUE
    A second request no longer overwrites the first. `request()` appends to a
    small FIFO and shows only the item at the front; `resolve()` answers the
    front and puts the next one on screen. That is what makes "shut the WiFi
    off and then hibernate the machine" work: the assistant asks both questions
    in one breath, the user answers them one at a time, and neither action is
    silently dropped — which is what happened when a slot could hold only one.

    The queue is deliberately short and refuses a duplicate key. It exists so
    that a sentence with two irreversible verbs in it survives; it is not a
    backlog, because a banner the user has to scroll through is a banner they
    stop reading. A repeated key is refused by `request()` itself, so callers
    no longer have to check `pending_title()` before asking.

WHAT BELONGS HERE AND WHAT DOES NOT
    Only genuinely irreversible things. Anything that can be reversed should be
    done at once and pushed onto core/undo.py instead — undo is faster than a
    question, and an assistant that asks before every action is one nobody uses.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# A pending confirmation is abandoned after this long. Chosen to outlast a
# normal "hang on, let me look at the screen" pause without leaving a live
# shutdown button sitting on the HUD for the rest of the day. Behind another
# request a timeout still runs from when that action was asked, not from when
# its turn came up — a user who never sees a question should not find it live
# an hour later.
TIMEOUT_SECONDS = 90.0

# How many unanswered questions will queue. Four is two sentences' worth with
# room to spare; past that the answers stop meaning anything.
MAX_QUEUE = 4


@dataclass
class _Pending:
    key:     str
    title:   str
    detail:  str
    run:     Callable[[], str]
    at:      float


_pending: list[_Pending] = []
_lock = threading.Lock()

# Key of whatever is on screen right now, '' when the HUD shows nothing. Kept so
# a second `resolve()` or a stray rebind cannot stack a banner on a banner.
_showing = ""

# Set once at startup by main.py. Signature: (title, detail) -> None for show,
# and () -> None for hide. Both are marshalled onto the Qt thread by the UI.
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb:  Optional[Callable[[str], None]] = None


def bind(show, hide, log=None) -> None:
    """Wire this module to the HUD. Called once from main.py at startup."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


def _purge_locked() -> list[str]:
    """Drop expired requests from the front; returns their titles.

    Only the front needs checking: timestamps increase down the queue, so a
    later entry cannot be older than the one ahead of it.
    """
    now = time.monotonic()
    expired = []
    while _pending and now - _pending[0].at > TIMEOUT_SECONDS:
        expired.append(_pending.pop(0).title)
    return expired


def _show_front() -> list[str]:
    """Put the oldest unanswered request on screen; returns titles that failed.

    Call with the lock free. Every callback leaves the lock, because `_show_cb`
    hands work to the Qt thread and `_log_cb` writes to a widget — holding the
    queue across either is how a UI repaint ends up blocking an audio thread.
    """
    global _showing
    failed = []
    while True:
        with _lock:
            expired = _purge_locked()
            if not _pending:
                _showing = ""
                break
            front = _pending[0]
            if front.key == _showing:
                break
            title, detail, key = front.title, front.detail, front.key
        for t in expired:
            _log(f"SYS: Confirmation expired — {t}")
        try:
            _show_cb(title, detail)
        except Exception as e:
            # The action stays unparked and un-run: a banner nobody can see is
            # a question nobody can answer, so the request is dropped here and
            # reported rather than left to time out in the dark.
            with _lock:
                if _pending and _pending[0].key == key:
                    _pending.pop(0)
            _log(f"ERR: Could not put '{title}' on screen — {e}. Nothing was done.")
            failed.append(title)
            continue
        with _lock:
            _showing = key
        break
    return failed


def request(key: str, title: str, detail: str, run: Callable[[], str]) -> str:
    """Park an irreversible action behind the on-screen gate.

    Returns the sentence the tool should hand back to the model — phrased as an
    instruction so the assistant asks the user out loud in their own language,
    rather than reading an English string verbatim."""
    if _show_cb is None:
        # No interface bound (headless, or a very early call). Refuse rather
        # than silently performing something irreversible.
        return (f"I cannot confirm '{title}' right now because the interface is "
                f"not available, so I have not done it.")

    duplicate = full = False
    with _lock:
        expired = _purge_locked()
        if any(p.key == key for p in _pending):
            duplicate = True
        elif len(_pending) >= MAX_QUEUE:
            full = True
        else:
            _pending.append(_Pending(key=key, title=title, detail=detail,
                                     run=run, at=time.monotonic()))

    for t in expired:
        _log(f"SYS: Confirmation expired — {t}")

    if duplicate:
        return (f"[ALREADY_PENDING] '{title}' is already waiting for the user on "
                f"screen. Do not ask again and do not do it yet; say that the "
                f"question is already up.")
    if full:
        return (f"[CONFIRMATION_QUEUE_FULL] {MAX_QUEUE} actions are already "
                f"waiting for confirmation, so '{title}' was not asked and "
                f"nothing was done. Tell the user to answer the ones on screen "
                f"first.")

    failed = _show_front()
    if title in failed:
        return (f"Could not put '{title}' on screen — the interface rejected it, "
                f"so nothing was done. Say that plainly; do not retry it.")

    with _lock:
        pos = next((i + 1 for i, p in enumerate(_pending) if p.key == key), 1)

    _log(f"SYS: Awaiting confirmation — {title}")
    if pos > 1:
        return (
            f"[CONFIRMATION_QUEUED] '{title}' is number {pos} in the queue "
            f"behind {pos - 1} other confirmation(s) already on screen. Say "
            f"ONE short sentence in the user's own language telling them you "
            f"need their confirmation on the HUD, in order, before you do "
            f"anything. Do not claim any of it is done."
        )
    return (
        f"[CONFIRMATION_PENDING] I have put a confirmation on screen for: {title}. "
        f"Say ONE short sentence in the user's own language telling them you need "
        f"them to confirm it on the HUD before you do it. Do not claim it is done."
    )


def resolve(accepted: bool) -> None:
    """Called by the UI when the user presses CONFIRM or CANCEL.

    Answers the request at the front of the queue — the one on screen — and then
    puts the next one up, if any.

    Runs the stored callable on a worker thread: this is invoked from the Qt
    thread, and shutting the machine down from inside a button handler would
    freeze the interface on its way out."""
    global _showing

    with _lock:
        p = _pending.pop(0) if _pending else None
        _showing = ""

    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass

    if p is not None:
        if time.monotonic() - p.at > TIMEOUT_SECONDS:
            _log(f"SYS: Confirmation expired — {p.title}")
        elif not accepted:
            _log(f"SYS: Cancelled — {p.title}")
        else:
            def _worker():
                try:
                    result = p.run() or "Done."
                    _log(f"SYS: Confirmed — {p.title}. {result}")
                except Exception as e:
                    _log(f"ERR: {p.title} failed — {e}")

            threading.Thread(target=_worker, daemon=True,
                             name=f"confirm-{p.key}").start()

    _show_front()


def pending_title() -> str:
    """The title on screen, '' when nothing is waiting.

    Kept for callers that want to know whether the user is already being asked
    something; `request()` refuses a duplicate key on its own, so it is no
    longer the way to avoid stacking two banners."""
    with _lock:
        _purge_locked()
        return _pending[0].title if _pending else ""


def pending_count() -> int:
    """How many irreversible actions are waiting for an answer."""
    with _lock:
        _purge_locked()
        return len(_pending)
