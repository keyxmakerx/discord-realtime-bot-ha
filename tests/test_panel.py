"""Tests for assistant.py's button handlers: which cell a button actually
points at when tapped, and what a requester is told when a swap ask can't be
delivered.

``assistant.py`` is imported for real (HA and discord are installed); the
assistant is built with ``__new__`` and given only the fields each handler reads.

Runnable with plain ``python3 tests/test_panel.py``.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from custom_components.laundry_discord import assistant as assist_mod  # noqa: E402
from custom_components.laundry_discord import plan as plan_mod  # noqa: E402
from custom_components.laundry_discord import trade as trade_mod  # noqa: E402
from custom_components.laundry_discord.const import (  # noqa: E402
    GRID_RECUR_CUSTOM_ID,
)


def _run(coro):
    return asyncio.run(coro)


TZ = datetime.timezone(datetime.timedelta(hours=-5))
# A Wednesday, so "today" differs from the Thursday cell tapped in tests.
WED = datetime.datetime(2026, 8, 5, 18, 0, tzinfo=TZ)
THU_EVE = "3-eve"


class _Entry:
    """A config entry with nothing on it but the house switches under test."""

    def __init__(self, options=None) -> None:
        self.data: dict = {}
        self.options = dict(options or {})


class _User:
    def __init__(self, user_id=42) -> None:
        self.id = user_id
        self.display_name = "Ada"


class _Interaction:
    def __init__(self, user_id=42) -> None:
        self.user = _User(user_id)
        self.followups: list = []
        self.followup = self

    async def send(self, content, **kwargs):
        self.followups.append(content)


def _assistant(now=WED, **state):
    """An assistant with the planner state a grid handler reads, and no Store."""
    a = assist_mod.LaundryAssistant.__new__(assist_mod.LaundryAssistant)
    a._entry = None
    a.bot = None
    a._people = {}
    a._overrides = {}
    a._history = []
    a._corrections = []
    a._budgets = {}
    a._trades = []
    a._grid_day = {}
    a._ask_cell = {}
    a._ask_offer = {}
    a._last_cell = {}
    a._nudge_cell = {}
    a._running_from = a._running_until = None
    a.__dict__.update(state)
    a._now = lambda: now
    a.saves: list = []

    async def _save():
        a.saves.append(True)

    a._async_save = _save
    # The render is captured rather than sent, since that's what tests check.
    a.rendered: list = []

    async def _respond(interaction, embed, view, *, edit):
        a.rendered.append(view)
        return True

    a._async_respond = _respond
    return a


def _standing(a, user_id=42) -> list[str]:
    """This person's standing weekly cells, as cell keys (translated from the
    stored [weekday, slot] pairs).
    """
    person = assist_mod.people_mod.get_person(a._people, user_id)
    return plan_mod.recurring_cells(person)


def _recur_button(view):
    """The ♻ button on a rendered grid, or None when it isn't offered."""
    for item in view.children:
        if getattr(item, "custom_id", None) == GRID_RECUR_CUSTOM_ID:
            return item
    return None


# --- ♻, and the cell it is actually pointing at -------------------------------
def test_the_recurring_button_is_retired_when_the_grid_changes_day() -> None:
    # _last_cell used to survive a day change, so ♻ stayed armed on a cell from
    # a different day than the one now on screen.
    a = _assistant()
    _run(a.async_pick_day(_Interaction(), 3))  # Thursday
    _run(a.async_toggle_cell(_Interaction(), "eve"))
    assert a.booked_cells(42) == [THU_EVE]
    assert _recur_button(a.rendered[-1]) is not None  # armed, on the day shown

    _run(a.async_pick_day(_Interaction(), 0))  # ...now looking at Monday
    assert _recur_button(a.rendered[-1]) is None, "♻ survived the day change"
    # And if it is tapped anyway (an old message still on somebody's screen),
    # nothing is promoted.
    _run(a.async_toggle_recurring(_Interaction()))
    assert _standing(a) == []


def test_the_recurring_button_is_retired_when_the_grid_is_reopened() -> None:
    # Reopening the grid (always on today) used to leave ♻ armed on an older
    # cell, so one tap could silently demote a slot never touched this session.
    a = _assistant()
    _run(a.async_pick_day(_Interaction(), 3))
    _run(a.async_toggle_cell(_Interaction(), "eve"))
    _run(a.async_toggle_recurring(_Interaction()))  # Thu Eve is now standing
    assert _standing(a) == [THU_EVE]

    _run(a.async_open_grid(_Interaction()))  # opens on today (Wednesday)
    assert _recur_button(a.rendered[-1]) is None, "♻ survived a reopen"
    _run(a.async_toggle_recurring(_Interaction()))
    assert _standing(a) == [THU_EVE], "a reopened grid demoted a slot nobody tapped"


def test_the_recurring_button_still_works_on_the_cell_just_tapped() -> None:
    # The normal tap-cell-then-♻ gesture must still work; this is a retirement,
    # not a removal.
    a = _assistant()
    _run(a.async_pick_day(_Interaction(), 3))
    _run(a.async_toggle_cell(_Interaction(), "eve"))
    _run(a.async_toggle_recurring(_Interaction()))
    assert _standing(a) == [THU_EVE]
    # ...and it is still offered, because the grid is still on that day.
    assert _recur_button(a.rendered[-1]) is not None


# --- a swap ask that could not be delivered -----------------------------------
def test_an_undeliverable_swap_ask_says_nothing_extra() -> None:
    # An undelivered ask must read exactly like every other holder-side
    # refusal: the grid note alone, no extra ephemeral revealing DM status.
    week = plan_mod.iso_week_key(WED)
    people = assist_mod.people_mod
    prefs = people.set_reminders({}, 42, people.REMIND_DM, name="Ada")
    prefs = people.set_reminders(prefs, 77, people.REMIND_DM, name="Bo")
    a = _assistant(
        _people=prefs,
        _overrides={week: {THU_EVE: ["77"], "1-am": ["42"]}},
        _ask_cell={"42": THU_EVE},
        _ask_offer={"42": "1-am"},
        # The house switch reads the config entry, and there isn't one here.
        _entry=_Entry({assist_mod.CONF_TRADES: True}),
    )

    async def _undeliverable(request):
        return False  # discord.Forbidden 50007, or a send that timed out

    a._async_deliver_request = _undeliverable
    interaction = _Interaction()
    _run(a.async_send_trade(interaction))
    assert interaction.followups == [], interaction.followups
    # It lapses rather than blocking the holder/slot for a week over an unseen message.
    assert len(a._trades) == 1
    assert trade_mod.is_open(a._trades[0], WED) is False
    # It still costs the asker what a delivered ask would, so the silence isn't a white lie.
    assert trade_mod.asked_this_week(a._trades, 42, THU_EVE, week) is True
    assert len(trade_mod.pending_from(a._trades, 42, WED)) == 1


def _run_all() -> None:
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"ok  {name}")
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"\n{count} passed")


if __name__ == "__main__":
    _run_all()
