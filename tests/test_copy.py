"""Tests that the panel copy and docs/design.md stay true to what the code does,
not just what it once did.

``assistant.py`` imports Home Assistant and ``discord``, so it's read with
:mod:`ast` rather than imported; ``nudge`` and ``trade`` (which decide the
facts being claimed) are loaded for real by file path.

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


# Loaded by file path in dependency order, so each module's relative imports
# fall back to bare ones and find their real neighbour.
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

with open(os.path.join(HERE, "..", "docs", "design.md"), encoding="utf-8") as _fh:
    _DESIGN = _fh.read()


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
    """Every string literal under ``node``; f-string segments count,
    interpolations don't (a computed value can't go stale, a literal can).
    """
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _fenced_block(marker: str) -> str:
    """The ``` block in docs/design.md containing ``marker``."""
    blocks = _DESIGN.split("```")[1::2]
    for block in blocks:
        if marker in block:
            return block
    raise AssertionError(f"no docs/design.md code block contains {marker!r}")


def _section(heading: str) -> str:
    """One docs/design.md section, from its heading to the next of any depth."""
    start = _DESIGN.index(heading)
    rest = _DESIGN[start + len(heading):]
    end = rest.find("\n#")
    return rest if end < 0 else rest[:end]


# --- the 🔔 panel's own claims ------------------------------------------------
def test_the_notify_panel_names_no_hour_a_house_option_can_move() -> None:
    """``nudge_lead`` (5min-3h) moves every heads-up clock time; the panel must
    not hard-code one, except 06:00 (the fixed AM window).
    """
    movable = set()
    for slot in _plan.SLOTS:
        for lead in range(_const.MIN_NUDGE_LEAD, _const.MAX_NUDGE_LEAD + 1):
            hour, minute = _nudge.heads_up_clock(slot, lead)
            movable.add(f"{hour:02d}:{minute:02d}")
    # Default and max lead, both reachable from the options flow.
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
    # "An hour before" is the same claim spelled out in words.
    assert "an hour" not in " ".join(copy).lower()
    # The one time it may name, because no option touches it.
    assert _plan.SLOT_WINDOWS[_plan.SLOT_AM][0] == 6


# --- docs/design.md's account of the panel ----------------------------------
def test_the_design_doc_draws_the_panel_the_code_actually_builds() -> None:
    """Walks ``AssistantView``'s buttons and ``_settings_embed``'s fields, and
    checks the design doc sketch shows each — a stale sketch once hid a real control.
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
    # ...plus emoji handed in at the call site (one class, three reminder-mode buttons).
    emojis |= {
        text
        for text in _strings(view)
        if 0 < len(text) <= 2 and not text.isascii()
    }
    sketch = _fenced_block("🤖 Your laundry assistant")
    for emoji in sorted(emojis):
        assert emoji in sketch, f"the design doc panel sketch is missing {emoji}"
    # The embed's fields, by name. The sketch is drawn with day-learning and
    # monitoring on, so conditional fields like "Guessing" are expected.
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
        assert field in sketch, f"the design doc panel sketch is missing {field!r}"


def test_the_design_doc_names_every_reason_a_swap_ask_is_not_delivered() -> None:
    """The refusal message is intentionally flat (one sentence, whatever the
    reason), so docs/design.md is the only place a reason is
    findable — every reason ``reachable`` can return must be listed there.
    """
    documented = {
        _trade.REASON_NOT_OPTED_IN: "never opened the 🤖 panel",
        _trade.REASON_REMINDERS_OFF: "reminders 🚫 off",
        _trade.REASON_NOT_DM: "on the channel default",
        _trade.REASON_DM_CLOSED: "DMs closed",
        _trade.REASON_PAUSED: "paused",
        _trade.REASON_SWAPS_OFF: "🔁 Swaps switched off",
        _trade.REASON_QUIET: "quiet hours",
        # Not a fact about the holder: an unreadable clock is the caller's own bug.
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
    guardrails = _section("### Who can be asked")
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
