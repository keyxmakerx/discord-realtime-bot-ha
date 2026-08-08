"""Tests for the copy that ships — the panel's own lines and the README's.

Every other test file here asks whether the integration *does* the right thing.
This one asks whether what it *says* about itself is true, which is a different
failure and a real one: a settings screen is read precisely when somebody is
deciding whether they need a setting, so a line that is wrong there talks them
out of the control that would have fixed their complaint. The 🔔 panel shipped
saying "the earliest I'd reach you is 05:00" — true only at the default
``nudge_lead``, and a house running the maximum lead of three hours DMs that
same person at 03:00.

``assistant.py`` imports Home Assistant and ``discord``, so it is read with
:mod:`ast` rather than imported, exactly as ``tests/test_sensor.py`` reads
``sensor.py``. The modules that decide the facts being claimed — ``nudge`` for
the trigger clock, ``trade`` for the reasons a swap ask is refused — are loaded
for real by file path, so nothing here is a second opinion about either.

Runnable with plain ``python3 tests/test_copy.py``.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.join(HERE, "..", "custom_components", "laundry_discord")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(PKG_DIR, filename)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Loaded by file path in dependency order, so the relative imports inside each
# module fall back to bare ones and find the real neighbour — see the same
# preamble in tests/test_reminders.py and tests/test_trade.py.
_const = _load("ld_const", "const.py")
sys.modules["const"] = _const
_plan = _load("ld_plan", "plan.py")
sys.modules["plan"] = _plan
_people = _load("ld_people", "people.py")
sys.modules["people"] = _people
_habit = _load("ld_habit", "habit.py")
sys.modules["habit"] = _habit
_nudge = _load("ld_nudge", "nudge.py")
sys.modules["nudge"] = _nudge
_trade = _load("ld_trade", "trade.py")

_ASSISTANT_PATH = os.path.join(PKG_DIR, "assistant.py")
with open(_ASSISTANT_PATH, encoding="utf-8") as _fh:
    _TREE = ast.parse(_fh.read(), filename=_ASSISTANT_PATH)

with open(os.path.join(HERE, "..", "README.md"), encoding="utf-8") as _fh:
    _README = _fh.read()


# --- reading assistant.py without importing it -------------------------------
def _node(name: str, tree=None):
    """The top-level class or function called ``name``."""
    for node in (tree or _TREE).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone from assistant.py")


def _method(class_name: str, name: str) -> ast.FunctionDef:
    for node in _node(class_name).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{class_name}.{name} is gone")


def _assignment(name: str) -> ast.expr:
    for node in _TREE.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    raise AssertionError(f"{name} is gone from assistant.py")


def _strings(node) -> list[str]:
    """Every string literal under ``node``.

    f-string *segments* count and interpolations do not, which is the whole
    point: a time the panel computes from :data:`plan.SLOT_WINDOWS` cannot go
    stale, and a time typed into the quotes can.
    """
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _fenced_block(marker: str) -> str:
    """The ``` block in the README containing ``marker``."""
    blocks = _README.split("```")[1::2]
    for block in blocks:
        if marker in block:
            return block
    raise AssertionError(f"no README code block contains {marker!r}")


def _section(heading: str) -> str:
    """One README section, from its heading to the next of any depth."""
    start = _README.index(heading)
    rest = _README[start + len(heading):]
    end = rest.find("\n#")
    return rest if end < 0 else rest[:end]


# --- the 🔔 panel's own claims ------------------------------------------------
def test_the_notify_panel_names_no_hour_a_house_option_can_move() -> None:
    """The bug this file was written for.

    The heads-up fires at the slot's start minus ``nudge_lead``, an option the
    config flow offers anywhere from 5 minutes to 3 hours — so *every* clock
    time in that range is a time some house's DM actually lands at, and none of
    them may be typed into a panel that never reads the option. 06:00 is
    different in kind: it is this repo's own AM window and no setting moves it,
    which is why the panel is allowed to name that one.
    """
    movable = set()
    for slot in _plan.SLOTS:
        for lead in range(_const.MIN_NUDGE_LEAD, _const.MAX_NUDGE_LEAD + 1):
            hour, minute = _nudge.heads_up_clock(slot, lead)
            movable.add(f"{hour:02d}:{minute:02d}")
    # The default lead and the maximum, both reachable from the options flow,
    # two hours apart on the one message that can wake somebody up.
    assert _nudge.heads_up_clock("am", _const.DEFAULT_NUDGE_LEAD) == (5, 0)
    assert _nudge.heads_up_clock("am", _const.MAX_NUDGE_LEAD) == (3, 0)
    assert {"05:00", "03:00"} <= movable

    copy = (
        _strings(_method("LaundryAssistant", "_notify_embed"))
        + _strings(_method("LaundryAssistant", "_notify_summary"))
        + _strings(_assignment("_NOTIFY_KINDS"))
    )
    for line in copy:
        for clock in sorted(movable):
            assert clock not in line, (
                f"the 🔔 panel says {clock!r}, which nudge_lead moves: {line!r}"
            )
    # ...and the same claim in words. "An hour before" is the default lead
    # spelled out, and wrong in the same houses for the same reason.
    assert "an hour" not in " ".join(copy).lower()
    # The one time it may name, because no option touches it.
    assert _plan.SLOT_WINDOWS[_plan.SLOT_AM][0] == 6


# --- the README's account of the panel ---------------------------------------
def test_the_readme_draws_the_panel_the_code_actually_builds() -> None:
    """A sketch of a panel is documentation people navigate by.

    Both times a button was added to 🤖 before this, the README sketch was
    updated in the same commit; the 🔔 one was not, and a reader looking for
    where to switch off the dawn heads-up found a panel with no such control on
    it. This walks ``AssistantView`` for the buttons a returning person gets and
    ``_settings_embed`` for the fields above them, and asks the sketch to
    contain each.
    """
    view = _method("AssistantView", "__init__")
    classes = {
        node.func.id
        for node in ast.walk(view)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.endswith("Button")
    }
    assert len(classes) >= 4, classes
    emojis = set()
    for name in classes:
        # The emoji a button hard-codes for itself...
        for node in ast.walk(_node(name)):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "emoji" and isinstance(kw.value, ast.Constant):
                        emojis.add(kw.value.value)
    # ...plus the ones handed in at the call site, which is how the three
    # reminder-mode buttons get theirs — one class, three emoji.
    emojis |= {
        text
        for text in _strings(view)
        if 0 < len(text) <= 2 and not text.isascii()
    }
    sketch = _fenced_block("🤖 Your laundry assistant")
    for emoji in sorted(emojis):
        assert emoji in sketch, f"the README panel sketch is missing {emoji}"
    # The embed's fields, by the names the code gives them. "Guessing" is
    # conditional in the code and drawn in the sketch, which is the state the
    # sketch is drawn in — day-learning on, monitoring on.
    fields = {
        kw.value.value
        for node in ast.walk(_method("LaundryAssistant", "_settings_embed"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_field"
        for kw in node.keywords
        if kw.arg == "name" and isinstance(kw.value, ast.Constant)
    }
    for field in sorted(fields):
        assert field in sketch, f"the README panel sketch is missing {field!r}"


def test_the_readme_names_every_reason_a_swap_ask_is_not_delivered() -> None:
    """§11: the refusal is flat, so the *documentation* is where they're listed.

    A requester is told the same sentence whichever of these it was, on purpose
    — a refusal that read differently would be a free oracle about a housemate
    they cannot even name. That makes the README the only place anybody can
    find out what stops an ask arriving, so every reason ``reachable`` can
    return has to be in the guardrail table. A reason with no entry below fails
    here rather than going quietly undocumented.
    """
    documented = {
        _trade.REASON_NOT_OPTED_IN: "never opened the 🤖 panel",
        _trade.REASON_REMINDERS_OFF: "reminders 🚫 off",
        _trade.REASON_NOT_DM: "on the channel default",
        _trade.REASON_DM_CLOSED: "with DMs closed",
        _trade.REASON_PAUSED: "paused",
        _trade.REASON_SWAPS_OFF: "🔁 **Swaps** switched off",
        _trade.REASON_QUIET: "quiet hours",
        # Not a fact about the holder at all: an unreadable clock is the
        # caller's bug, and nothing a housemate could have set or unset.
        _trade.REASON_MOMENT: None,
    }
    with open(os.path.join(PKG_DIR, "trade.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    returned = set()
    for name in ("reachable", "_delivery_gate"):
        for node in ast.walk(_node(name, tree)):
            if (
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Name)
                and node.value.id.startswith("REASON_")
            ):
                returned.add(getattr(_trade, node.value.id))
    returned.discard(_trade.REASON_OK)
    assert _trade.REASON_SWAPS_OFF in returned and _trade.REASON_QUIET in returned
    guardrails = _section("#### Every guardrail, spelled out")
    for reason in sorted(returned):
        assert reason in documented, f"{reason} is undocumented and unlisted here"
        phrase = documented[reason]
        if phrase is None:
            continue
        assert phrase in guardrails, f"the guardrail table never mentions {reason}"


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
