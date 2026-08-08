"""Tests for the panel's own hands — the bits of assistant.py that write.

``tests/test_plan.py`` and ``tests/test_trade.py`` cover the rules; ``ast`` is
what ``tests/test_copy.py`` uses to read the wording. What neither can reach is
the layer in between: which cell a button is pointing at by the time somebody
taps it, and what the requester is *told* when a swap ask cannot be delivered.
Both of the cases here write something permanent — a standing weekly slot the
whole house then sees, and a fact about a housemate's privacy settings — on a
tap that meant something else.

``assistant.py`` imports Home Assistant and ``discord`` for real, and both are
installed, so the module is imported the way HA imports it. The assistant itself
is built with ``__new__`` and given only the fields the handler under test
reads: ``__init__`` opens a ``Store``, which none of this depends on.

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
# A Wednesday, so "today" and the Thursday cell the tests tap are different days
# — which is the whole hazard: a button armed on one day, tapped on another.
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
    # Every handler ends by drawing something; what it drew is what the tests
    # read, so the render is captured rather than sent.
    a.rendered: list = []

    async def _respond(interaction, embed, view, *, edit):
        a.rendered.append(view)
        return True

    a._async_respond = _respond
    return a


def _standing(a, user_id=42) -> list[str]:
    """This person's standing weekly cells, as cell keys.

    ``person["slots"]`` stores ``[weekday, slot]`` pairs (§12); the tests speak
    in cell keys, and :func:`plan.recurring_cells` is the one translation.
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
    # REGRESSION: _last_cell was set by async_toggle_cell and never cleared.
    # async_pick_day and async_open_grid clear _ask_cell — retiring 🔁, whose own
    # docstring says a swap button pointing at Thursday while the buttons
    # underneath say Monday is exactly how it asks about a slot somebody didn't
    # mean — but left _last_cell alone, and _async_render_grid adds ♻ for any
    # cell the viewer holds regardless of which day is on screen.
    #
    # So: open 📅 on Wednesday, pick Thursday, tap Eve, then look at Monday. The
    # grid shows Monday's four slot buttons and, beside them, ♻ Every week —
    # and tapping it makes *Thursday* Eve a standing weekly slot, drawn as ║ to
    # the whole house every week from then on.
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
    # REGRESSION, and the worse direction of the same bug: 📅 always opens on
    # today, so after leaving the panel and coming back the grid shows today's
    # slots — while ♻ was still armed on a cell tapped days earlier, now
    # labelled "Just this week". One tap, with no cell tap before it in this
    # session at all, silently cancelled a standing slot.
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
    # The gesture the button exists for — tap a cell, tap ♻ — is untouched, so
    # the fix above is a retirement and not a removal.
    a = _assistant()
    _run(a.async_pick_day(_Interaction(), 3))
    _run(a.async_toggle_cell(_Interaction(), "eve"))
    _run(a.async_toggle_recurring(_Interaction()))
    assert _standing(a) == [THU_EVE]
    # ...and it is still offered, because the grid is still on that day.
    assert _recur_button(a.rendered[-1]) is not None


# --- a swap ask that could not be delivered -----------------------------------
def test_an_undeliverable_swap_ask_says_nothing_extra() -> None:
    # REGRESSION: trade.py lists REASON_UNDELIVERED among HOLDER_REASONS, whose
    # entire point is that "all of it renders as one identical sentence" — but
    # async_send_trade rendered it as an extra ephemeral *on top of* sent_text,
    # an outcome no other holder-side refusal produces. The first time a
    # holder's DMs turned out to be closed, the requester saw "🔁 Asked..."
    # immediately followed by "I can't ask about that one right now", and had
    # learned, for a cell they can watch across weeks, that its holder has DMs
    # from server members turned off.
    #
    # Every other holder-side condition — swaps off, quiet hours, blocked,
    # paused, 💬 channel, budget spent, a known-closed inbox — leaves the grid
    # note standing alone, so this one must too.
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
    # The ask really was attempted and really is gone — it lapses rather than
    # blocking the holder and the slot for a week over a message nobody saw...
    assert len(a._trades) == 1
    assert trade_mod.is_open(a._trades[0], WED) is False
    # ...and it still costs the asker exactly what a delivered ask costs them,
    # which is what makes the silence honest rather than a white lie.
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
