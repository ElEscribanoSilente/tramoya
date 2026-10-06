"""Tests for tramoya.py — comprehensive coverage."""

import copy
import json
import pickle
import threading
import warnings
import pytest
from tramoya import (
    Machine, MachineBuilder, SubMachine, ParallelMachine,
    MachineError, InvalidTransition, GuardRejected,
)
from tramoya import LintFinding
from tramoya import _Transition


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_order(**kwargs):
    defaults = dict(
        states=["draft", "submitted", "approved", "rejected"],
        transitions=[
            ("submit",  "draft",     "submitted"),
            ("approve", "submitted", "approved", lambda ctx: ctx.get("score", 0) > 50),
            ("reject",  "submitted", "rejected"),
            ("revise",  "rejected",  "draft"),
        ],
        initial="draft",
    )
    defaults.update(kwargs)
    return Machine(**defaults)


# ─── Basic transitions ──────────────────────────────────────────────────────

class TestBasicTransitions:
    def test_initial_state(self):
        m = make_order()
        assert m.state == "draft"
        assert m.initial == "draft"

    def test_simple_trigger(self):
        m = make_order()
        result = m.trigger("submit")
        assert result == "submitted"
        assert m.state == "submitted"

    def test_invalid_trigger(self):
        m = make_order()
        with pytest.raises(InvalidTransition) as exc_info:
            m.trigger("approve")
        assert exc_info.value.trigger == "approve"
        assert exc_info.value.state == "draft"

    def test_chain_transitions(self):
        m = make_order()
        m.trigger("submit")
        m.trigger("approve", score=80)
        assert m.state == "approved"

    def test_invalid_initial_state(self):
        with pytest.raises(MachineError, match="not in states"):
            Machine(states=["a", "b"], transitions=[], initial="c")

    def test_invalid_state_in_transition(self):
        with pytest.raises(MachineError, match="not declared"):
            Machine(
                states=["a", "b"],
                transitions=[("go", "a", "nonexistent")],
                initial="a",
            )

    def test_invalid_source_in_transition(self):
        with pytest.raises(MachineError, match="not declared"):
            Machine(
                states=["a", "b"],
                transitions=[("go", "nonexistent", "b")],
                initial="a",
            )


# ─── Guards ──────────────────────────────────────────────────────────────────

class TestGuards:
    def test_guard_passes(self):
        m = make_order()
        m.trigger("submit")
        m.trigger("approve", score=80)
        assert m.state == "approved"

    def test_guard_rejects(self):
        m = make_order()
        m.trigger("submit")
        with pytest.raises(GuardRejected) as exc_info:
            m.trigger("approve", score=30)
        assert exc_info.value.trigger == "approve"
        assert m.state == "submitted"

    def test_can_with_guard(self):
        m = make_order()
        m.trigger("submit")
        assert m.can("approve", score=80) is True
        assert m.can("approve", score=30) is False
        assert m.can("nonexistent") is False

    def test_multiple_guards_first_wins(self):
        m = Machine(
            states=["idle", "high", "low"],
            transitions=[
                ("eval", "idle", "high", lambda ctx: ctx.get("val", 0) >= 50),
                ("eval", "idle", "low",  lambda ctx: ctx.get("val", 0) < 50),
            ],
            initial="idle",
        )
        m.trigger("eval", val=80)
        assert m.state == "high"

    def test_multiple_guards_second_wins(self):
        m = Machine(
            states=["idle", "high", "low"],
            transitions=[
                ("eval", "idle", "high", lambda ctx: ctx.get("val", 0) >= 50),
                ("eval", "idle", "low",  lambda ctx: ctx.get("val", 0) < 50),
            ],
            initial="idle",
        )
        m.trigger("eval", val=20)
        assert m.state == "low"


# ─── Context ────────────────────────────────────────────────────────────────

class TestContext:
    def test_ctx_initial(self):
        m = Machine(
            states=["a"], transitions=[], initial="a",
            ctx={"key": "value"},
        )
        assert m.ctx == {"key": "value"}

    def test_ctx_merge_on_trigger(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
        )
        m.trigger("go", foo="bar")
        assert m.ctx["foo"] == "bar"

    def test_ctx_NOT_mutated_on_invalid_transition(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
        )
        m.trigger("go")
        assert m.state == "b"
        with pytest.raises(InvalidTransition):
            m.trigger("go", leaked="yes")
        assert "leaked" not in m.ctx

    def test_ctx_NOT_mutated_on_guard_rejected(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b", lambda ctx: False)],
            initial="a",
        )
        with pytest.raises(GuardRejected):
            m.trigger("go", leaked="yes")
        assert "leaked" not in m.ctx
        assert m.state == "a"


# ─── Hooks (on_enter / on_exit) ─────────────────────────────────────────────

class TestHooks:
    def test_on_enter_fires(self):
        log = []
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
            on_enter={"b": lambda ctx: log.append("entered_b")},
        )
        m.trigger("go")
        assert log == ["entered_b"]

    def test_on_exit_fires(self):
        log = []
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
            on_exit={"a": lambda ctx: log.append("exited_a")},
        )
        m.trigger("go")
        assert log == ["exited_a"]

    def test_execution_order(self):
        log = []
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b", None, lambda ctx: log.append("action"))],
            initial="a",
            on_enter={"b": lambda ctx: log.append("enter_b")},
            on_exit={"a": lambda ctx: log.append("exit_a")},
        )
        m.trigger("go")
        assert log == ["exit_a", "action", "enter_b"]

    def test_on_enter_invalid_state_raises(self):
        with pytest.raises(MachineError, match="on_enter references unknown state"):
            Machine(
                states=["a", "b"],
                transitions=[],
                initial="a",
                on_enter={"typo": lambda ctx: None},
            )

    def test_on_exit_invalid_state_raises(self):
        with pytest.raises(MachineError, match="on_exit references unknown state"):
            Machine(
                states=["a", "b"],
                transitions=[],
                initial="a",
                on_exit={"typo": lambda ctx: None},
            )

    def test_hook_exception_doesnt_corrupt_history(self):
        def bad_exit(ctx):
            raise ValueError("boom")

        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
            on_exit={"a": bad_exit},
        )
        with pytest.raises(ValueError):
            m.trigger("go")
        # State should NOT have changed since exit hook failed before state change
        # History should be clean
        assert m.state == "a"
        assert m.history == []

    def test_action_exception_doesnt_corrupt_history(self):
        def bad_action(ctx):
            raise ValueError("boom")

        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b", None, bad_action)],
            initial="a",
        )
        with pytest.raises(ValueError):
            m.trigger("go")
        assert m.state == "a"
        assert m.history == []


# ─── History / Undo ─────────────────────────────────────────────────────────

class TestHistory:
    def test_undo(self):
        m = make_order()
        m.trigger("submit")
        m.trigger("approve", score=80)
        assert m.state == "approved"
        m.undo()
        assert m.state == "submitted"
        m.undo()
        assert m.state == "draft"

    def test_undo_empty_raises(self):
        m = make_order()
        with pytest.raises(MachineError, match="Nothing to undo"):
            m.undo()

    def test_history_property(self):
        m = make_order()
        m.trigger("submit")
        m.trigger("approve", score=80)
        assert m.history == ["draft", "submitted"]

    def test_history_is_copy(self):
        m = make_order()
        m.trigger("submit")
        h = m.history
        h.clear()
        assert m.history == ["draft"]

    def test_history_size_limit(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b"), ("back", "b", "a")],
            initial="a",
            history_size=2,
        )
        m.trigger("go")    # history: [a]
        m.trigger("back")  # history: [a, b]
        m.trigger("go")    # history: [b, a] — first 'a' dropped
        assert len(m.history) == 2

    def test_history_disabled(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
            history_size=0,
        )
        m.trigger("go")
        assert m.history == []
        with pytest.raises(MachineError):
            m.undo()


# ─── Reset ───────────────────────────────────────────────────────────────────

class TestReset:
    def test_reset_to_initial(self):
        m = make_order()
        m.trigger("submit")
        m.reset()
        assert m.state == "draft"
        assert m.history == []

    def test_reset_to_specific_state(self):
        m = make_order()
        m.trigger("submit")
        m.reset("rejected")
        assert m.state == "rejected"

    def test_reset_invalid_state(self):
        m = make_order()
        with pytest.raises(MachineError, match="Unknown state"):
            m.reset("nonexistent")

    def test_reset_empty_state_name_rejected(self):
        """Empty string is not a valid state name."""
        with pytest.raises(MachineError, match="non-empty strings"):
            Machine(states=["", "a"], transitions=[], initial="")

    def test_reset_non_string_state_rejected(self):
        """Non-string state names are rejected."""
        with pytest.raises(MachineError, match="non-empty strings"):
            Machine(states=[123, "a"], transitions=[], initial="a")  # type: ignore[list-item]


# ─── Introspection ──────────────────────────────────────────────────────────

class TestIntrospection:
    def test_available_triggers(self):
        m = make_order()
        assert "submit" in m.available_triggers
        assert "approve" not in m.available_triggers

    def test_available_triggers_no_duplicates(self):
        m = Machine(
            states=["a", "b", "c"],
            transitions=[
                ("go", "a", "b"),
                ("go", "a", "c"),
            ],
            initial="a",
        )
        assert m.available_triggers.count("go") == 1

    def test_is_final(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
        )
        assert m.is_final is False
        m.trigger("go")
        assert m.is_final is True

    def test_states_property(self):
        m = make_order()
        assert m.states == {"draft", "submitted", "approved", "rejected"}

    def test_states_is_copy(self):
        m = make_order()
        s = m.states
        s.add("fake")
        assert "fake" not in m.states


# ─── Wildcard transitions ───────────────────────────────────────────────────

class TestWildcard:
    def test_wildcard_from_any_state(self):
        m = Machine(
            states=["a", "b", "cancelled"],
            transitions=[
                ("go",     "a", "b"),
                ("cancel", "*", "cancelled"),
            ],
            initial="a",
        )
        m.trigger("cancel")
        assert m.state == "cancelled"

    def test_wildcard_from_different_state(self):
        m = Machine(
            states=["a", "b", "cancelled"],
            transitions=[
                ("go",     "a", "b"),
                ("cancel", "*", "cancelled"),
            ],
            initial="a",
        )
        m.trigger("go")
        m.trigger("cancel")
        assert m.state == "cancelled"

    def test_wildcard_in_available_triggers(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("cancel", "*", "b")],
            initial="a",
        )
        assert "cancel" in m.available_triggers

    def test_wildcard_with_guard(self):
        m = Machine(
            states=["a", "error"],
            transitions=[("fail", "*", "error", lambda ctx: ctx.get("critical"))],
            initial="a",
        )
        assert m.can("fail", critical=False) is False
        assert m.can("fail", critical=True) is True


# ─── Internal transitions ───────────────────────────────────────────────────

class TestInternal:
    def test_internal_no_state_change(self):
        log = []
        m = Machine(
            states=["a", "b"],
            transitions=[
                ("go",  "a", "b"),
                ("log", "a", None, None, lambda ctx: log.append("logged")),
            ],
            initial="a",
        )
        m.trigger("log")
        assert m.state == "a"
        assert log == ["logged"]

    def test_internal_no_history(self):
        m = Machine(
            states=["a"],
            transitions=[("ping", "a", None)],
            initial="a",
        )
        m.trigger("ping")
        assert m.history == []

    def test_internal_no_enter_exit(self):
        log = []
        m = Machine(
            states=["a"],
            transitions=[("ping", "a", None)],
            initial="a",
            on_enter={"a": lambda ctx: log.append("enter")},
            on_exit={"a": lambda ctx: log.append("exit")},
        )
        m.trigger("ping")
        assert log == []


# ─── trigger_many ────────────────────────────────────────────────────────────

class TestTriggerMany:
    def test_batch_triggers(self):
        m = make_order()
        result = m.trigger_many("submit", ("approve", {"score": 99}))
        assert result == "approved"

    def test_batch_stops_on_failure(self):
        m = make_order()
        with pytest.raises(GuardRejected):
            m.trigger_many("submit", ("approve", {"score": 10}))
        assert m.state == "submitted"  # first succeeded, second failed


# ─── Observers ───────────────────────────────────────────────────────────────

class TestObservers:
    def test_on_transition(self):
        log = []
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
            on_transition=lambda t, s, d, c: log.append((t, s, d)),
        )
        m.trigger("go")
        assert log == [("go", "a", "b")]

    def test_subscribe_unsubscribe(self):
        log = []
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b"), ("back", "b", "a")],
            initial="a",
        )
        cb = m.subscribe(lambda t, s, d, c: log.append(t))
        m.trigger("go")
        assert log == ["go"]

        m.unsubscribe(cb)
        m.trigger("back")
        assert log == ["go"]  # no new entry

    def test_observer_on_internal(self):
        log = []
        m = Machine(
            states=["a"],
            transitions=[("ping", "a", None)],
            initial="a",
            on_transition=lambda t, s, d, c: log.append(t),
        )
        m.trigger("ping")
        assert log == ["ping"]


# ─── Serialization ──────────────────────────────────────────────────────────

class TestSerialization:
    def test_to_dict_copies(self):
        m = make_order()
        m.trigger("submit")
        d = m.to_dict()
        d["ctx"]["injected"] = True
        d["history"].clear()
        assert "injected" not in m.ctx
        assert len(m.history) == 1

    def test_round_trip_json(self):
        m = make_order()
        m.trigger("submit")
        m.trigger("approve", score=80)
        snap = m.to_json()

        m2 = make_order()
        m2.load_dict(json.loads(snap))
        assert m2.state == "approved"
        assert m2.ctx["score"] == 80
        assert m2.history == ["draft", "submitted"]

    def test_load_dict_validates_state(self):
        m = make_order()
        with pytest.raises(MachineError, match="Unknown state"):
            m.load_dict({"state": "fake", "ctx": {}, "history": []})

    def test_load_dict_validates_history(self):
        m = make_order()
        with pytest.raises(MachineError, match="Unknown state.*in history"):
            m.load_dict({"state": "draft", "ctx": {}, "history": ["fake"]})

    def test_from_dict(self):
        data = {"state": "submitted", "ctx": {"a": 1}, "history": ["draft"]}
        m = Machine.from_dict(
            states=["draft", "submitted", "approved", "rejected"],
            transitions=[("submit", "draft", "submitted")],
            data=data,
        )
        assert m.state == "submitted"
        assert m.ctx == {"a": 1}
        assert m.history == ["draft"]

    def test_from_dict_validates_history(self):
        data = {"state": "draft", "ctx": {}, "history": ["bad"]}
        with pytest.raises(MachineError, match="Unknown state.*in history"):
            Machine.from_dict(
                states=["draft", "submitted"],
                transitions=[],
                data=data,
            )

    def test_from_json(self):
        data = json.dumps({"state": "submitted", "ctx": {}, "history": ["draft"]})
        m = Machine.from_json(
            states=["draft", "submitted"],
            transitions=[("submit", "draft", "submitted")],
            json_str=data,
        )
        assert m.state == "submitted"


# ─── Graph export ────────────────────────────────────────────────────────────

class TestGraphExport:
    def test_to_dot_basic(self):
        m = make_order()
        dot = m.to_dot("test")
        assert 'digraph "test" {' in dot
        assert "draft" in dot
        assert "submitted" in dot

    def test_to_dot_escapes_quotes(self):
        m = Machine(
            states=['say "hi"', "ok"],
            transitions=[("go", 'say "hi"', "ok")],
            initial='say "hi"',
        )
        dot = m.to_dot()
        assert '\\"' in dot
        assert "digraph" in dot

    def test_to_dot_wildcard_expands(self):
        m = Machine(
            states=["a", "b", "err"],
            transitions=[("fail", "*", "err")],
            initial="a",
        )
        dot = m.to_dot()
        assert '"a" -> "err"' in dot
        assert '"b" -> "err"' in dot
        assert "dashed" in dot

    def test_to_mermaid_basic(self):
        m = make_order()
        mmd = m.to_mermaid()
        assert "stateDiagram-v2" in mmd
        assert "draft" in mmd

    def test_to_mermaid_wildcard_expands(self):
        m = Machine(
            states=["a", "b", "err"],
            transitions=[("fail", "*", "err")],
            initial="a",
        )
        mmd = m.to_mermaid()
        # Should NOT contain [*] for wildcards
        assert "[*]" not in mmd
        assert "a --> err" in mmd
        assert "b --> err" in mmd


# ─── __eq__ ──────────────────────────────────────────────────────────────────

class TestEquality:
    def test_equal_machines(self):
        m1 = make_order()
        m2 = make_order()
        assert m1 == m2

    def test_different_state(self):
        m1 = make_order()
        m2 = make_order()
        m2.trigger("submit")
        assert m1 != m2

    def test_not_equal_to_other_type(self):
        m = make_order()
        assert m != "not a machine"


# ─── MachineBuilder ─────────────────────────────────────────────────────────

class TestMachineBuilder:
    def test_basic_build(self):
        b = MachineBuilder("off")
        b.add_states("off", "on")
        b.transition("toggle", "off", "on")
        b.transition("toggle", "on", "off")
        m = b.build()
        m.trigger("toggle")
        assert m.state == "on"

    def test_initial_auto_added(self):
        b = MachineBuilder("idle")
        b.add_states("running")
        b.transition("start", "idle", "running")
        m = b.build()
        assert "idle" in m.states
        assert m.state == "idle"

    def test_deduplicate_states(self):
        b = MachineBuilder("a")
        b.add_states("a", "b")
        b.add_states("b", "c")
        m = b.build()
        assert m.states == {"a", "b", "c"}

    def test_guard_decorator(self):
        b = MachineBuilder("a")
        b.add_states("a", "b")

        @b.guard("go", "a", "b")
        def check(ctx):
            return ctx.get("ok", False)

        b.transition("go", "a", "b")
        m = b.build()
        assert m.can("go") is False
        assert m.can("go", ok=True) is True

    def test_action_decorator(self):
        log = []
        b = MachineBuilder("a")
        b.add_states("a", "b")

        @b.on("go", "a", "b")
        def on_go(ctx):
            log.append("went")

        m = b.build()
        m.trigger("go")
        assert log == ["went"]

    def test_enter_exit_decorators(self):
        log = []
        b = MachineBuilder("a")
        b.add_states("a", "b")
        b.transition("go", "a", "b")

        @b.enter("b")
        def enter_b(ctx):
            log.append("enter_b")

        @b.exit("a")
        def exit_a(ctx):
            log.append("exit_a")

        m = b.build()
        m.trigger("go")
        assert log == ["exit_a", "enter_b"]

    def test_observe_decorator(self):
        log = []
        b = MachineBuilder("a")
        b.add_states("a", "b")
        b.transition("go", "a", "b")

        @b.observe
        def on_any(trigger, src, dst, ctx):
            log.append((trigger, src, dst))

        m = b.build()
        m.trigger("go")
        assert log == [("go", "a", "b")]

    def test_on_transition_handler_decorator(self):
        log = []
        b = MachineBuilder("a")
        b.add_states("a", "b")
        b.transition("go", "a", "b")

        @b.on_transition_handler
        def handler(trigger, src, dst, ctx):
            log.append(trigger)

        m = b.build()
        m.trigger("go")
        assert log == ["go"]


# ─── SubMachine ──────────────────────────────────────────────────────────────

class TestSubMachine:
    def test_basic_submachine(self):
        inner = Machine(
            states=["s1", "s2"],
            transitions=[("next", "s1", "s2")],
            initial="s1",
        )
        sub = SubMachine("processing", inner)

        parent = Machine(
            states=["idle", "processing", "done"],
            transitions=[
                ("start",  "idle",       "processing"),
                ("finish", "processing", "done"),
            ],
            initial="idle",
            on_enter={"processing": sub.enter},
            on_exit={"processing": sub.exit},
        )

        parent.trigger("start")
        assert sub.machine.state == "s1"
        sub.trigger("next")
        assert sub.machine.state == "s2"

    def test_submachine_resets_on_enter(self):
        inner = Machine(
            states=["s1", "s2"],
            transitions=[("next", "s1", "s2")],
            initial="s1",
        )
        sub = SubMachine("processing", inner)

        parent = Machine(
            states=["idle", "processing", "done"],
            transitions=[
                ("start",  "idle",       "processing"),
                ("finish", "processing", "idle"),
            ],
            initial="idle",
            on_enter={"processing": sub.enter},
            on_exit={"processing": sub.exit},
        )

        parent.trigger("start")
        sub.trigger("next")
        assert sub.machine.state == "s2"

        parent.trigger("finish")
        parent.trigger("start")
        assert sub.machine.state == "s1"  # reset on re-enter

    def test_submachine_shared_keys(self):
        inner = Machine(
            states=["s1"],
            transitions=[],
            initial="s1",
        )
        sub = SubMachine("processing", inner, shared_keys=["user_id"])

        parent = Machine(
            states=["idle", "processing"],
            transitions=[("start", "idle", "processing")],
            initial="idle",
            on_enter={"processing": sub.enter},
            ctx={"user_id": "alice", "secret": "password"},
        )

        parent.trigger("start")
        assert sub.machine.ctx.get("user_id") == "alice"
        assert "secret" not in sub.machine.ctx

    def test_submachine_no_shared_keys_no_contamination(self):
        inner = Machine(
            states=["s1"],
            transitions=[],
            initial="s1",
        )
        sub = SubMachine("processing", inner)

        parent = Machine(
            states=["idle", "processing"],
            transitions=[("start", "idle", "processing")],
            initial="idle",
            on_enter={"processing": sub.enter},
            ctx={"parent_data": "should_not_leak"},
        )

        parent.trigger("start")
        assert "parent_data" not in sub.machine.ctx

    def test_submachine_restore(self):
        inner = Machine(
            states=["s1", "s2"],
            transitions=[("next", "s1", "s2")],
            initial="s1",
        )
        sub = SubMachine("processing", inner)

        parent = Machine(
            states=["idle", "processing"],
            transitions=[
                ("start",  "idle",       "processing"),
                ("pause",  "processing", "idle"),
            ],
            initial="idle",
            on_enter={"processing": sub.enter},
            on_exit={"processing": sub.exit},
        )

        parent.trigger("start")
        sub.trigger("next")
        assert sub.machine.state == "s2"

        parent.trigger("pause")
        sub.restore()
        assert sub.machine.state == "s2"


# ─── Edge cases ──────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_self_loop(self):
        log = []
        m = Machine(
            states=["a"],
            transitions=[("ping", "a", "a")],
            initial="a",
            on_enter={"a": lambda ctx: log.append("enter")},
            on_exit={"a": lambda ctx: log.append("exit")},
        )
        m.trigger("ping")
        assert m.state == "a"
        assert log == ["exit", "enter"]

    def test_transition_dataclass(self):
        from tramoya import _Transition
        tr = _Transition("go", "a", "b")
        m = Machine(
            states=["a", "b"],
            transitions=[tr],
            initial="a",
        )
        m.trigger("go")
        assert m.state == "b"

    def test_repr(self):
        m = make_order()
        r = repr(m)
        assert "Machine" in r
        assert "draft" in r

    def test_submachine_repr(self):
        inner = Machine(states=["s1"], transitions=[], initial="s1")
        sub = SubMachine("processing", inner)
        r = repr(sub)
        assert "SubMachine" in r
        assert "processing" in r


# ─── v1.3 fixes ─────────────────────────────────────────────────────────────

class TestBuilderDeterminism:
    """#1: MachineBuilder.build() must produce deterministic transition order."""

    def test_builder_transition_order_matches_registration(self):
        """Plain transitions come first, in registration order."""
        b = MachineBuilder("a")
        b.add_states("a", "b", "c")
        b.transition("go", "a", "b")
        b.transition("go", "a", "c")
        m = b.build()
        # First registered wins (a->b before a->c)
        m.trigger("go")
        assert m.state == "b"

    def test_builder_guard_vs_plain_order(self):
        """Guard-only transitions come after plain transitions."""
        b = MachineBuilder("a")
        b.add_states("a", "b", "c")

        @b.guard("go", "a", "c")
        def always_true(ctx):
            return True

        b.transition("go", "a", "b")  # plain, registered after guard

        m = b.build()
        # Plain transition (a->b) should come first in output
        m.trigger("go")
        assert m.state == "b"

    def test_builder_deterministic_across_runs(self):
        """Same builder config always produces same behavior."""
        results = []
        for _ in range(20):
            b = MachineBuilder("a")
            b.add_states("a", "high", "low")

            @b.guard("eval", "a", "high")
            def check_high(ctx):
                return ctx.get("val", 0) >= 50

            @b.guard("eval", "a", "low")
            def check_low(ctx):
                return ctx.get("val", 0) < 50

            m = b.build()
            m.trigger("eval", val=80)
            results.append(m.state)

        assert all(r == results[0] for r in results), f"Non-deterministic: {set(results)}"


class TestSubMachineCtxCleanup:
    """#2: SubMachine.enter() must clear inner ctx."""

    def test_ctx_cleared_on_reentry(self):
        inner = Machine(
            states=["s1", "s2"],
            transitions=[("next", "s1", "s2")],
            initial="s1",
        )
        sub = SubMachine("processing", inner)

        parent = Machine(
            states=["idle", "processing"],
            transitions=[
                ("start",  "idle",       "processing"),
                ("stop",   "processing", "idle"),
            ],
            initial="idle",
            on_enter={"processing": sub.enter},
            on_exit={"processing": sub.exit},
        )

        parent.trigger("start")
        sub.trigger("next", data="leftover")
        assert sub.machine.ctx.get("data") == "leftover"

        parent.trigger("stop")
        parent.trigger("start")
        # ctx must be clean after re-entry
        assert "data" not in sub.machine.ctx


class TestIsStuck:
    """#3: is_stuck() evaluates guards, unlike is_final."""

    def test_is_stuck_all_guards_fail(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b", lambda ctx: ctx.get("allowed", False))],
            initial="a",
        )
        assert m.is_final is False  # has transitions structurally
        assert m.is_stuck() is True  # but none can fire

    def test_is_stuck_with_kwargs(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b", lambda ctx: ctx.get("allowed", False))],
            initial="a",
        )
        assert m.is_stuck(allowed=True) is False

    def test_is_stuck_no_transitions(self):
        m = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a")
        m.trigger("go")
        assert m.is_final is True
        assert m.is_stuck() is True


class TestExplicitVsWildcardPriority:
    """#4: Explicit transitions must beat wildcards."""

    def test_explicit_wins_over_wildcard(self):
        m = Machine(
            states=["a", "b", "c"],
            transitions=[
                ("go", "*", "c"),    # wildcard registered first
                ("go", "a", "b"),    # explicit registered second
            ],
            initial="a",
        )
        m.trigger("go")
        assert m.state == "b"  # explicit wins regardless of registration order

    def test_wildcard_fires_when_no_explicit(self):
        m = Machine(
            states=["a", "b", "fallback"],
            transitions=[
                ("go", "a", "b"),
                ("go", "*", "fallback"),
            ],
            initial="a",
        )
        m.trigger("go")
        assert m.state == "b"  # explicit for "a"
        m.reset("b")

        # Hypothetical - no explicit for "b", wildcard should work
        # but "go" from "b" has no explicit, only wildcard
        # We need a state that has no explicit "go"
        m2 = Machine(
            states=["a", "b", "fallback"],
            transitions=[
                ("go", "a", "b"),
                ("go", "*", "fallback"),
            ],
            initial="b",
        )
        m2.trigger("go")
        assert m2.state == "fallback"  # wildcard kicks in


class TestTransitionAtomicity:
    """#5: Callback exceptions must not leave partial state."""

    def test_on_exit_exception_full_rollback(self):
        def bad_exit(ctx):
            raise RuntimeError("exit failed")

        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
            on_exit={"a": bad_exit},
        )
        m.ctx["preserved"] = True
        with pytest.raises(RuntimeError):
            m.trigger("go", new_key="val")
        assert m.state == "a"
        assert m.history == []
        assert m.ctx.get("preserved") is True
        # kwargs should be rolled back too
        assert "new_key" not in m.ctx

    def test_action_exception_full_rollback(self):
        def bad_action(ctx):
            raise RuntimeError("action failed")

        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b", None, bad_action)],
            initial="a",
            ctx={"original": True},
        )
        with pytest.raises(RuntimeError):
            m.trigger("go", added="val")
        assert m.state == "a"
        assert m.ctx == {"original": True}

    def test_on_enter_exception_full_rollback(self):
        def bad_enter(ctx):
            raise RuntimeError("enter failed")

        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
            on_enter={"b": bad_enter},
            ctx={"key": "original"},
        )
        with pytest.raises(RuntimeError):
            m.trigger("go", key="modified")
        assert m.state == "a"
        assert m.ctx == {"key": "original"}
        assert m.history == []

    def test_internal_action_exception_rollback(self):
        def bad_action(ctx):
            raise RuntimeError("boom")

        m = Machine(
            states=["a"],
            transitions=[("ping", "a", None, None, bad_action)],
            initial="a",
            ctx={"val": 1},
        )
        with pytest.raises(RuntimeError):
            m.trigger("ping", val=999)
        assert m.ctx == {"val": 1}


class TestUndoRevertsCtx:
    """#6: undo() must revert both state AND context."""

    def test_undo_restores_ctx(self):
        m = Machine(
            states=["a", "b", "c"],
            transitions=[("go", "a", "b"), ("go", "b", "c")],
            initial="a",
            ctx={"step": 0},
        )
        m.trigger("go", step=1)
        assert m.state == "b"
        assert m.ctx["step"] == 1

        m.trigger("go", step=2)
        assert m.state == "c"
        assert m.ctx["step"] == 2

        m.undo()
        assert m.state == "b"
        assert m.ctx["step"] == 1

        m.undo()
        assert m.state == "a"
        assert m.ctx["step"] == 0

    def test_undo_ctx_isolation(self):
        """Undo snapshot must not be affected by later ctx mutations."""
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
            ctx={"list": [1]},
        )
        m.trigger("go")
        m.ctx["list"].append(2)  # mutate after transition

        m.undo()
        assert m.ctx["list"] == [1]  # snapshot was taken before mutation


class TestDotInternalSelfLoop:
    """#7: Internal transitions should be self-loops, not fake nodes."""

    def test_dot_internal_is_self_loop(self):
        m = Machine(
            states=["a"],
            transitions=[("ping", "a", None)],
            initial="a",
        )
        dot = m.to_dot()
        assert "(internal)" not in dot
        assert '"a" -> "a"' in dot
        assert "dotted" in dot

    def test_dot_wildcard_internal_no_fake_nodes(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("log", "*", None)],
            initial="a",
        )
        dot = m.to_dot()
        assert "(internal)" not in dot
        assert '"a" -> "a"' in dot
        assert '"b" -> "b"' in dot


class TestLoadDictDefensiveCopy:
    """#8: load_dict() must copy data defensively."""

    def test_load_dict_does_not_alias_input(self):
        data = {"state": "a", "ctx": {"key": "val"}, "history": []}
        m = Machine(states=["a", "b"], transitions=[], initial="a")
        m.load_dict(data)
        data["ctx"]["injected"] = True
        assert "injected" not in m.ctx

    def test_load_dict_validates_type(self):
        m = Machine(states=["a"], transitions=[], initial="a")
        with pytest.raises(MachineError):
            m.load_dict({"state": 123, "ctx": {}, "history": []})

    def test_load_dict_legacy_history_format(self):
        """Backwards compatible with old string-only history."""
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
        )
        m.load_dict({"state": "b", "ctx": {}, "history": ["a"]})
        assert m.state == "b"
        assert m.history == ["a"]

    def test_load_dict_new_history_format(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
        )
        m.load_dict({
            "state": "b",
            "ctx": {"x": 1},
            "history": [{"state": "a", "ctx": {"x": 0}}],
        })
        assert m.state == "b"
        assert m.history == ["a"]
        m.undo()
        assert m.state == "a"
        assert m.ctx == {"x": 0}

    def test_serialization_round_trip_with_ctx_history(self):
        """Full round-trip: trigger, serialize, deserialize, undo restores ctx."""
        m1 = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
            ctx={"val": 0},
        )
        m1.trigger("go", val=1)
        snap = m1.to_json()

        m2 = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
        )
        m2.load_dict(json.loads(snap))
        assert m2.state == "b"
        m2.undo()
        assert m2.state == "a"
        assert m2.ctx == {"val": 0}


# ─── v1.4: Frozen guards ────────────────────────────────────────────────────

class TestFrozenGuards:
    """Guards receive read-only ctx to prevent accidental mutation."""

    def test_guard_cannot_mutate_ctx(self):
        def bad_guard(ctx):
            try:
                ctx["injected"] = True  # MappingProxyType raises TypeError
            except TypeError:
                pass
            return True

        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b", bad_guard)],
            initial="a",
        )
        m.trigger("go")
        assert "injected" not in m.ctx

    def test_can_guard_cannot_mutate(self):
        mutated = [False]
        def sneaky_guard(ctx):
            try:
                ctx["hack"] = True
            except TypeError:
                mutated[0] = True
            return True

        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b", sneaky_guard)],
            initial="a",
        )
        m.can("go")
        assert mutated[0] is True
        assert "hack" not in m.ctx

    def test_guard_can_read_kwargs(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b", lambda ctx: ctx.get("key") == "val")],
            initial="a",
        )
        assert m.can("go", key="val") is True
        assert m.can("go", key="other") is False


# ─── v1.4: ParallelMachine ──────────────────────────────────────────────────

class TestParallelMachine:
    """Orthogonal regions running in parallel."""

    def _make_parallel(self):
        movement = Machine(
            states=["idle", "walking", "running"],
            transitions=[
                ("walk", "idle", "walking"),
                ("run",  "walking", "running"),
                ("stop", "walking", "idle"),
                ("stop", "running", "idle"),
            ],
            initial="idle",
        )
        combat = Machine(
            states=["peaceful", "attacking", "defending"],
            transitions=[
                ("attack", "peaceful", "attacking"),
                ("defend", "peaceful", "defending"),
                ("peace",  "attacking", "peaceful"),
                ("peace",  "defending", "peaceful"),
            ],
            initial="peaceful",
        )
        return ParallelMachine(movement=movement, combat=combat)

    def test_initial_state(self):
        p = self._make_parallel()
        assert p.state == {"movement": "idle", "combat": "peaceful"}

    def test_trigger_one_region(self):
        p = self._make_parallel()
        p.trigger("walk")
        assert p.state == {"movement": "walking", "combat": "peaceful"}

    def test_trigger_other_region(self):
        p = self._make_parallel()
        p.trigger("attack")
        assert p.state == {"movement": "idle", "combat": "attacking"}

    def test_independent_regions(self):
        p = self._make_parallel()
        p.trigger("walk")
        p.trigger("attack")
        assert p.state == {"movement": "walking", "combat": "attacking"}

    def test_broadcast_trigger(self):
        """A trigger that exists in multiple regions fires in all of them."""
        m1 = Machine(
            states=["on", "off"],
            transitions=[("reset", "on", "off"), ("start", "off", "on")],
            initial="on",
        )
        m2 = Machine(
            states=["on", "off"],
            transitions=[("reset", "on", "off"), ("start", "off", "on")],
            initial="on",
        )
        p = ParallelMachine(a=m1, b=m2)
        p.trigger("reset")
        assert p.state == {"a": "off", "b": "off"}

    def test_trigger_no_region_handles_raises(self):
        p = self._make_parallel()
        with pytest.raises(InvalidTransition):
            p.trigger("nonexistent")

    def test_trigger_region(self):
        p = self._make_parallel()
        p.trigger_region("movement", "walk")
        assert p.state["movement"] == "walking"
        assert p.state["combat"] == "peaceful"

    def test_trigger_region_unknown_raises(self):
        p = self._make_parallel()
        with pytest.raises(MachineError, match="Unknown region"):
            p.trigger_region("nonexistent", "walk")

    def test_can(self):
        p = self._make_parallel()
        assert p.can("walk") is True
        assert p.can("attack") is True
        assert p.can("fly") is False

    def test_undo_all(self):
        p = self._make_parallel()
        p.trigger("walk")
        p.trigger("attack")
        p.undo()
        assert p.state == {"movement": "idle", "combat": "peaceful"}

    def test_undo_specific_region(self):
        p = self._make_parallel()
        p.trigger("walk")
        p.trigger("attack")
        p.undo(region="movement")
        assert p.state == {"movement": "idle", "combat": "attacking"}

    def test_undo_unknown_region_raises(self):
        p = self._make_parallel()
        with pytest.raises(MachineError):
            p.undo(region="nonexistent")

    def test_reset(self):
        p = self._make_parallel()
        p.trigger("walk")
        p.trigger("attack")
        p.reset()
        assert p.state == {"movement": "idle", "combat": "peaceful"}

    def test_serialization(self):
        p = self._make_parallel()
        p.trigger("walk")
        p.trigger("attack")
        data = p.to_dict()

        p2 = self._make_parallel()
        p2.load_dict(data)
        assert p2.state == {"movement": "walking", "combat": "attacking"}

    def test_load_dict_unknown_region_raises(self):
        p = self._make_parallel()
        with pytest.raises(MachineError, match="Unknown region"):
            p.load_dict({"fake": {"state": "a", "ctx": {}, "history": []}})

    def test_repr(self):
        p = self._make_parallel()
        r = repr(p)
        assert "ParallelMachine" in r
        assert "idle" in r
        assert "peaceful" in r

    def test_empty_raises(self):
        with pytest.raises(MachineError):
            ParallelMachine()

    def test_ctx_per_region(self):
        m1 = Machine(states=["a"], transitions=[], initial="a", ctx={"x": 1})
        m2 = Machine(states=["b"], transitions=[], initial="b", ctx={"y": 2})
        p = ParallelMachine(r1=m1, r2=m2)
        assert p.ctx == {"r1": {"x": 1}, "r2": {"y": 2}}


# ─── 1.5.0: initial preserved across round-trip (bug #1) ────────────────────

class TestInitialPreservedRoundTrip:
    def test_to_dict_includes_initial(self):
        m = make_order()
        m.trigger("submit")
        d = m.to_dict()
        assert d["initial"] == "draft"
        assert d["state"] == "submitted"

    def test_reset_after_round_trip_returns_to_initial(self):
        m = make_order()
        m.trigger("submit")
        snap = m.to_json()

        m2 = Machine.from_json(
            states=["draft", "submitted", "approved", "rejected"],
            transitions=[
                ("submit",  "draft",     "submitted"),
                ("approve", "submitted", "approved"),
                ("reject",  "submitted", "rejected"),
                ("revise",  "rejected",  "draft"),
            ],
            json_str=snap,
        )
        assert m2.state == "submitted"
        assert m2.initial == "draft"
        m2.reset()
        assert m2.state == "draft"  # not "submitted"

    def test_legacy_snapshot_without_initial_still_loads(self):
        # Pre-1.5.0 snapshots had no "initial" key. They must continue to
        # load — initial falls back to data["state"], matching pre-1.5.0
        # (buggy) behavior. No data loss for upgrading users.
        legacy = {"state": "submitted", "ctx": {"a": 1}, "history": ["draft"]}
        m = Machine.from_dict(
            states=["draft", "submitted", "approved", "rejected"],
            transitions=[("submit", "draft", "submitted")],
            data=legacy,
        )
        assert m.state == "submitted"
        assert m.initial == "submitted"  # legacy fallback

    def test_load_dict_validates_initial(self):
        m = make_order()
        with pytest.raises(MachineError, match="Unknown initial state"):
            m.load_dict({"state": "draft", "initial": "fake", "ctx": {}, "history": []})

    def test_load_dict_restores_initial(self):
        m = make_order()
        m.load_dict({"state": "submitted", "initial": "submitted", "ctx": {}, "history": []})
        assert m.initial == "submitted"


# ─── 1.5.0: Mermaid label escape (bug #5) ───────────────────────────────────

class TestMermaidLabelEscape:
    def _machine_with_trigger(self, trigger_name: str) -> Machine:
        return Machine(
            states=["a", "b"],
            transitions=[(trigger_name, "a", "b")],
            initial="a",
        )

    def test_alphanumeric_unchanged(self):
        m = self._machine_with_trigger("submit")
        out = m.to_mermaid()
        assert ": submit" in out
        # No HTML entities should appear for plain triggers.
        assert "&" not in out

    def test_colon_escaped(self):
        m = self._machine_with_trigger("foo:bar")
        out = m.to_mermaid()
        assert "&#58;" in out
        assert "foo:bar" not in out  # raw colon must not appear

    def test_pipe_escaped(self):
        m = self._machine_with_trigger("a|b")
        out = m.to_mermaid()
        assert "&#124;" in out

    def test_angle_brackets_escaped(self):
        m = self._machine_with_trigger("price>50")
        out = m.to_mermaid()
        assert "&gt;" in out
        # Make sure raw ">" doesn't appear in the label region.
        for line in out.splitlines():
            if "price" in line:
                # Only the "-->" arrow is allowed to contain ">".
                assert ">50" not in line.split("-->", 1)[1]

    def test_ampersand_escaped_first(self):
        # "&" must be replaced before the entities we insert, otherwise
        # "&amp;#58;" would appear instead of "&amp;" + "&#58;".
        m = self._machine_with_trigger("a&b:c")
        out = m.to_mermaid()
        assert "&amp;" in out
        assert "&#58;" in out
        # Must NOT contain double-encoded form.
        assert "&amp;#58;" not in out

    def test_quote_and_newline_escaped(self):
        m = self._machine_with_trigger('a"b\nc')
        out = m.to_mermaid()
        assert "&quot;" in out
        assert "<br/>" in out


# ─── 1.5.0: Dispatch index preserves observable behavior (bug #6) ──────────

class TestDispatchIndex:
    def test_available_triggers_preserves_registration_order(self):
        m = Machine(
            states=["a", "b", "c"],
            transitions=[
                ("zebra", "a", "b"),
                ("apple", "a", "c"),
                ("mango", "a", "b"),
            ],
            initial="a",
        )
        assert m.available_triggers == ["zebra", "apple", "mango"]

    def test_explicit_beats_wildcard_priority(self):
        # _match_candidates must return explicit-source transitions before
        # wildcard-source ones. Trigger "go" has both; firing it should land
        # in "explicit_target" not "wildcard_target".
        m = Machine(
            states=["a", "explicit_target", "wildcard_target"],
            transitions=[
                ("go", "*", "wildcard_target"),
                ("go", "a", "explicit_target"),
            ],
            initial="a",
        )
        m.trigger("go")
        assert m.state == "explicit_target"

    def test_dispatch_handles_wildcard_only_triggers(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("cancel", "*", "b")],
            initial="a",
        )
        assert "cancel" in m.available_triggers
        m.trigger("cancel")
        assert m.state == "b"


# ─── 1.5.0: shallow_ctx flag (bug #4) ───────────────────────────────────────

class TestShallowCtx:
    def test_default_is_deepcopy(self):
        # Mutating a nested list inside an action and then undoing must
        # restore the pre-mutation state when shallow_ctx=False (default).
        def append_to_list(ctx):
            ctx["items"].append("new")

        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b", None, append_to_list)],
            initial="a",
            ctx={"items": ["original"]},
        )
        m.trigger("go")
        assert m.ctx["items"] == ["original", "new"]
        m.undo()
        assert m.ctx["items"] == ["original"]  # protected by deepcopy

    def test_shallow_ctx_does_not_protect_nested(self):
        # With shallow_ctx=True, the snapshot aliases the same list object.
        # Mutating it in-place during action persists across undo. This is
        # documented behavior — opt-in tradeoff.
        def append_to_list(ctx):
            ctx["items"].append("new")

        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b", None, append_to_list)],
            initial="a",
            ctx={"items": ["original"]},
            shallow_ctx=True,
        )
        m.trigger("go")
        m.undo()
        assert m.ctx["items"] == ["original", "new"]  # NOT protected — by design

    def test_shallow_ctx_protects_top_level_keys(self):
        # Top-level keys (added/removed via dict.update) ARE protected — the
        # dict() snapshot is its own dict, so undo restores the key set.
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
            ctx={"x": 1},
            shallow_ctx=True,
        )
        m.trigger("go", y=2)
        assert m.ctx == {"x": 1, "y": 2}
        m.undo()
        assert m.ctx == {"x": 1}


# ─── 1.5.0: transition_to (item A) ──────────────────────────────────────────

class TestTransitionTo:
    def test_basic(self):
        m = make_order()
        result = m.transition_to("submitted")
        assert result == "submitted"
        assert m.state == "submitted"

    def test_via_wildcard(self):
        m = Machine(
            states=["a", "b", "c"],
            transitions=[
                ("step", "a", "b"),
                ("cancel", "*", "c"),
            ],
            initial="a",
        )
        m.transition_to("c")
        assert m.state == "c"

    def test_unknown_dest_raises(self):
        m = make_order()
        with pytest.raises(InvalidTransition) as exc:
            m.transition_to("nonexistent")
        assert exc.value.reason == "unknown_state"

    def test_no_edge_raises(self):
        m = make_order()  # at "draft"
        # "approved" is reachable but only from "submitted" — no edge from
        # current state.
        with pytest.raises(InvalidTransition) as exc:
            m.transition_to("approved")
        assert exc.value.reason == "no_edge"

    def test_guard_blocks_raises_guard_rejected(self):
        m = make_order()
        m.trigger("submit")  # at "submitted"
        # "approve" guard requires score>50; transition_to should fail.
        with pytest.raises(GuardRejected) as exc:
            m.transition_to("approved", score=30)
        assert exc.value.reason == "guard_rejected"

    def test_kwargs_forwarded_to_trigger(self):
        m = make_order()
        m.trigger("submit")
        m.transition_to("approved", score=80)
        assert m.state == "approved"

    def test_overloaded_trigger_does_not_silently_dispatch_wrong_edge(self):
        # Two transitions share the trigger name "go" from state "a":
        # one to "b" (guard: flag=True), one to "c" (no guard, fallback).
        # Calling trigger("go") with flag=True goes to "b".
        # Calling transition_to("c") with flag=True must NOT silently fire
        # the trigger and land in "b" — it must raise.
        m = Machine(
            states=["a", "b", "c"],
            transitions=[
                ("go", "a", "b", lambda ctx: ctx.get("flag", False)),
                ("go", "a", "c"),
            ],
            initial="a",
        )
        # Sanity: trigger("go", flag=True) lands in "b"
        m2 = Machine(
            states=["a", "b", "c"],
            transitions=[
                ("go", "a", "b", lambda ctx: ctx.get("flag", False)),
                ("go", "a", "c"),
            ],
            initial="a",
        )
        m2.trigger("go", flag=True)
        assert m2.state == "b"

        # transition_to("c") with flag=True: higher-priority edge wins for "b",
        # so transition_to("c") cannot deterministically reach "c" → raise.
        with pytest.raises(GuardRejected):
            m.transition_to("c", flag=True)
        assert m.state == "a"  # unchanged

    def test_overloaded_trigger_when_higher_priority_edge_blocked(self):
        # Same setup as above but flag=False — the b-bound edge's guard fails,
        # so trigger("go") naturally falls through to the c-bound edge.
        # transition_to("c") should commit.
        m = Machine(
            states=["a", "b", "c"],
            transitions=[
                ("go", "a", "b", lambda ctx: ctx.get("flag", False)),
                ("go", "a", "c"),
            ],
            initial="a",
        )
        m.transition_to("c", flag=False)
        assert m.state == "c"


# ─── 1.5.0: deprecation warnings (#7, #8) ───────────────────────────────────

class TestDeprecationWarnings:
    def test_load_dict_warns_on_truncation(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
            history_size=2,
        )
        data = {
            "state": "a",
            "ctx": {},
            "history": [
                {"state": "a", "ctx": {}},
                {"state": "b", "ctx": {}},
                {"state": "a", "ctx": {}},
                {"state": "b", "ctx": {}},
                {"state": "a", "ctx": {}},
            ],
        }
        with pytest.warns(DeprecationWarning, match="history truncated"):
            m.load_dict(data)
        # Truncation still happens (silent → warning, not silent → raise yet).
        assert len(m.history) == 2

    def test_load_dict_no_warning_when_within_limit(self):
        m = Machine(
            states=["a", "b"],
            transitions=[("go", "a", "b")],
            initial="a",
            history_size=5,
        )
        data = {
            "state": "a",
            "ctx": {},
            "history": [{"state": "a", "ctx": {}}, {"state": "b", "ctx": {}}],
        }
        import warnings as _w
        with _w.catch_warnings():
            _w.simplefilter("error")  # promote any warning to exception
            m.load_dict(data)  # must not raise

    def test_from_dict_warns_on_ctx_kwarg(self):
        with pytest.warns(DeprecationWarning, match="ctx kwarg"):
            Machine.from_dict(
                states=["a", "b"],
                transitions=[("go", "a", "b")],
                data={"state": "a", "ctx": {"x": 1}, "history": []},
                ctx={"y": 2},  # ignored, warns
            )

    def test_from_dict_no_warning_without_ctx_kwarg(self):
        import warnings as _w
        with _w.catch_warnings():
            _w.simplefilter("error")
            Machine.from_dict(
                states=["a", "b"],
                transitions=[("go", "a", "b")],
                data={"state": "a", "ctx": {"x": 1}, "history": []},
            )


# ─── 1.5.0: typed reason field on exceptions (item E) ──────────────────────

class TestExceptionReason:
    def test_invalid_transition_default_reason(self):
        m = make_order()
        with pytest.raises(InvalidTransition) as exc:
            m.trigger("nonexistent")
        assert exc.value.reason == "no_edge"

    def test_guard_rejected_default_reason(self):
        m = make_order()
        m.trigger("submit")
        with pytest.raises(GuardRejected) as exc:
            m.trigger("approve", score=30)
        assert exc.value.reason == "guard_rejected"

    def test_str_unchanged_for_invalid_transition(self):
        # Critical for the additive contract: code that parses str(exc) must
        # continue to work in 1.5.0.
        m = make_order()
        try:
            m.trigger("nonexistent")
        except InvalidTransition as e:
            assert str(e) == "No transition 'nonexistent' from state 'draft'"

    def test_str_unchanged_for_guard_rejected(self):
        m = make_order()
        m.trigger("submit")
        try:
            m.trigger("approve", score=30)
        except GuardRejected as e:
            assert str(e) == "Guard rejected 'approve' from 'submitted'"

    def test_transition_to_unknown_state_reason(self):
        m = make_order()
        with pytest.raises(InvalidTransition) as exc:
            m.transition_to("ghost")
        assert exc.value.reason == "unknown_state"

    def test_reason_is_writable_in_constructor(self):
        # Subclasses or callers can construct with custom reason.
        e = InvalidTransition("foo", "bar", reason="unknown_state")
        assert e.reason == "unknown_state"
        assert e.trigger == "foo"
        assert e.state == "bar"


# ─── Audit 2026-07 regression anchors (A1/A2/A3) ────────────────────────────

class TestAuditA1HistoryRollback:
    """A1: a failed transition must fully roll back — including history — even
    when the history deque is already at maxlen. Previously the len-comparison
    pop guard failed to fire because append() at maxlen evicts the oldest entry
    without changing len, leaving a bogus entry and losing a real one."""

    def test_failed_transition_at_maxlen_preserves_history(self):
        def boom(ctx):
            raise RuntimeError("enter failed")

        m = Machine(
            states=["a", "b", "c", "d"],
            transitions=[("go_b", "a", "b"), ("go_c", "b", "c"), ("go_d", "c", "d")],
            initial="a",
            on_enter={"d": boom},
            history_size=2,
        )
        m.trigger("go_b")   # history: [a]
        m.trigger("go_c")   # history: [a, b] — full at maxlen=2
        assert m.history == ["a", "b"]

        with pytest.raises(RuntimeError):
            m.trigger("go_d")   # on_enter(d) raises -> full rollback

        assert m.state == "c"
        assert m.history == ["a", "b"]      # identical to before the failure
        assert m.undo() == "b"               # real previous state, not a no-op
        assert m.undo() == "a"

    def test_failed_transition_at_maxlen_size1(self):
        def boom(ctx):
            raise RuntimeError("boom")

        m = Machine(
            states=["a", "b", "c"],
            transitions=[("go", "a", "b"), ("fail", "b", "c")],
            initial="a",
            on_enter={"c": boom},
            history_size=1,
        )
        m.trigger("go")     # history: [a] — full at maxlen=1
        with pytest.raises(RuntimeError):
            m.trigger("fail")
        assert m.state == "b"
        assert m.history == ["a"]
        assert m.undo() == "a"


class TestAuditA2LoadDictAtomic:
    """A2: load_dict must validate before mutating (all-or-nothing) and wrap
    malformed input in MachineError instead of leaking raw KeyError/TypeError."""

    def _machine(self):
        m = Machine(states=["a", "b"], transitions=[("go", "a", "b")],
                    initial="a", ctx={"keep": "me"})
        m.trigger("go")
        return m

    def test_invalid_ctx_type_is_machine_error_and_atomic(self):
        m = self._machine()
        before_state, before_ctx = m.state, dict(m.ctx)
        with pytest.raises(MachineError):
            m.load_dict({"state": "a", "ctx": 12345, "history": []})
        assert m.state == before_state       # not committed
        assert m.ctx == before_ctx           # not wiped

    def test_missing_state_key_is_machine_error(self):
        m = self._machine()
        with pytest.raises(MachineError):
            m.load_dict({"ctx": {}, "history": []})

    def test_non_dict_data_is_machine_error(self):
        m = self._machine()
        for bad in ([], "x", 42, None):
            with pytest.raises(MachineError):
                m.load_dict(bad)

    def test_history_not_a_list_is_machine_error(self):
        m = self._machine()
        with pytest.raises(MachineError):
            m.load_dict({"state": "a", "ctx": {}, "history": "ab"})

    def test_invalid_history_ctx_type_is_machine_error(self):
        m = self._machine()
        with pytest.raises(MachineError):
            m.load_dict({"state": "a", "ctx": {},
                         "history": [{"state": "a", "ctx": 5}]})

    def test_uncopyable_ctx_is_machine_error_and_atomic(self):
        # Blind-review finding: deepcopy of an uncopyable value must surface as
        # MachineError (not a raw TypeError) and leave the machine untouched.
        class Boom:
            def __deepcopy__(self, memo):
                raise RuntimeError("no copy")

        m = self._machine()
        before_state, before_ctx = m.state, dict(m.ctx)
        with pytest.raises(MachineError):
            m.load_dict({"state": "a", "ctx": {"x": Boom()}, "history": []})
        assert m.state == before_state
        assert m.ctx == before_ctx
        with pytest.raises(MachineError):
            m.load_dict({"state": "a", "ctx": {},
                         "history": [{"state": "a", "ctx": {"x": Boom()}}]})
        assert m.state == before_state
        assert m.ctx == before_ctx

    def test_parallel_load_dict_atomic_on_region_failure(self):
        mk_a = lambda: Machine(states=["x", "y"], transitions=[("go", "x", "y")], initial="x")
        mk_b = lambda: Machine(states=["p", "q"], transitions=[("go", "p", "q")], initial="p")
        pm = ParallelMachine(rA=mk_a(), rB=mk_b())
        pm.trigger("go")                      # {rA: y, rB: q}
        before = pm.state
        with pytest.raises(MachineError):
            pm.load_dict({"rA": {"state": "x", "ctx": {}, "history": []},
                          "rB": {"state": "ZZZ", "ctx": {}, "history": []}})
        assert pm.state == before             # rA not mutated because rB failed

    def test_parallel_load_dict_non_dict_is_machine_error(self):
        pm = ParallelMachine(rA=Machine(states=["x"], transitions=[], initial="x"))
        with pytest.raises(MachineError):
            pm.load_dict("not a dict")


class TestAuditA3LoadDictDeepCopy:
    """A3: load_dict must deep-copy nested ctx so the input dict cannot alias
    internal state, matching the documented 'full transactional safety'."""

    def test_nested_ctx_not_aliased(self):
        data = {"state": "b", "ctx": {"items": [1, 2, 3]}, "history": []}
        m = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a")
        m.load_dict(data)
        data["ctx"]["items"].append(999)      # mutate input AFTER load
        assert m.ctx["items"] == [1, 2, 3]

    def test_nested_history_ctx_not_aliased(self):
        data = {"state": "b", "ctx": {},
                "history": [{"state": "a", "ctx": {"items": [1]}}]}
        m = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a")
        m.load_dict(data)
        data["history"][0]["ctx"]["items"].append(2)
        m.undo()
        assert m.ctx["items"] == [1]


class TestAuditMediums:
    """Audit 2026-07 MEDIUM findings (M3, M5-M12), each an executed-PoC anchor."""

    def test_m3_history_size_zero_drops_snapshot_history(self):
        m = Machine(states=["a", "b"], transitions=[("go", "a", "b")],
                    initial="a", history_size=0)
        m.load_dict({"state": "a", "ctx": {},
                     "history": [{"state": "a", "ctx": {}} for _ in range(1000)]})
        assert m.history == []                      # not loaded unbounded

    def test_m6_machine_is_hashable(self):
        m = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a")
        assert hash(m) == hash(m)
        assert m in {m}
        assert {m: 1}[m] == 1

    def test_m6_hash_is_by_identity_not_value(self):
        # Documented trade-off: hashing is by identity, so two value-equal
        # machines are distinct keys (a set does not dedupe them by value).
        m1 = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a")
        m2 = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a")
        assert m1 == m2                              # value-equal
        assert hash(m1) != hash(m2)                  # but hashed by identity
        assert len({m1, m2}) == 2                    # so a set keeps both

    def test_m7_eq_ignores_guard_behavior(self):
        # Documented: topology compares has-guard, not guard behavior.
        m1 = Machine(states=["a", "b"], transitions=[("go", "a", "b", lambda c: True)], initial="a")
        m2 = Machine(states=["a", "b"], transitions=[("go", "a", "b", lambda c: False)], initial="a")
        assert m1 == m2

    def test_m5_eq_distinguishes_history_size(self):
        m1 = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a", history_size=5)
        m2 = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a", history_size=50)
        assert m1 != m2

    def test_m5_eq_distinguishes_shallow_ctx(self):
        m1 = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a", shallow_ctx=True)
        m2 = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a", shallow_ctx=False)
        assert m1 != m2

    def test_m8_observer_unsubscribe_during_notify_not_skipped(self):
        m = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a")
        fired = []

        def obs_a(t, s, d, c):
            fired.append("A")
            m.unsubscribe(obs_a)

        def obs_b(t, s, d, c):
            fired.append("B")

        m.subscribe(obs_a)
        m.subscribe(obs_b)
        m.trigger("go")
        assert fired == ["A", "B"]                   # B not skipped

    def test_m9_observer_exception_propagates_after_commit(self):
        def boom(t, s, d, c):
            raise RuntimeError("boom")

        m = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a")
        m.subscribe(boom)
        with pytest.raises(RuntimeError):
            m.trigger("go")
        assert m.state == "b"                        # committed despite raise

    def test_m10_transition_to_single_eval_no_wrong_dest(self):
        calls = {"n": 0}

        def g(ctx):
            calls["n"] += 1
            return calls["n"] == 1                   # True only on first eval

        m = Machine(
            states=["s", "D", "WRONG"],
            transitions=[("go", "s", "D", g), ("go", "s", "WRONG", None)],
            initial="s",
        )
        assert m.transition_to("D") == "D"           # not "WRONG"
        assert calls["n"] == 1                        # single guard evaluation

    def test_m11_to_dot_title_quoted(self):
        m = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a")
        assert 'digraph "order-machine" {' in m.to_dot("order-machine")

    def test_m12_mermaid_ids_do_not_collide(self):
        m = Machine(states=["a-b", "a_b"], transitions=[("t", "a-b", "a_b")], initial="a-b")
        ids = m._mermaid_ids()
        assert ids["a-b"] != ids["a_b"]              # distinct nodes, not merged
        mmd = m.to_mermaid()
        assert 'as ' in mmd                           # real names declared


class TestAuditNitpicks:
    """Audit 2026-07 minor findings that changed code (L3, L4, L6, L8)."""

    def test_l3_baseexception_rolls_back_ctx(self):
        def boom(ctx):
            raise KeyboardInterrupt("ctrl-c")

        m = Machine(states=["a", "b"], transitions=[("go", "a", "b", None, boom)],
                    initial="a", ctx={"tag": "start"})
        with pytest.raises(KeyboardInterrupt):
            m.trigger("go", tag="mutated")
        assert m.state == "a"
        assert m.ctx == {"tag": "start"}             # rolled back despite BaseException

    def test_l4_shadowed_edge_reason_without_guards(self):
        # Internal edge (dest=None) shadows the explicit edge to D — no guards.
        m = Machine(states=["s", "D"], transitions=[("go", "s", None), ("go", "s", "D")],
                    initial="s")
        with pytest.raises(GuardRejected) as exc:
            m.transition_to("D")
        assert exc.value.reason == "no_deterministic_edge"

    def test_l4_guard_rejection_keeps_guard_rejected_reason(self):
        m = Machine(states=["s", "D"], transitions=[("go", "s", "D", lambda c: False)],
                    initial="s")
        with pytest.raises(GuardRejected) as exc:
            m.transition_to("D")
        assert exc.value.reason == "guard_rejected"

    def test_l6_unsubscribe_is_idempotent(self):
        m = Machine(states=["a", "b"], transitions=[("go", "a", "b")], initial="a")

        def cb(t, s, d, c):
            pass

        m.subscribe(cb)
        m.unsubscribe(cb)
        m.unsubscribe(cb)                            # double unsubscribe: no ValueError
        m.unsubscribe(lambda t, s, d, c: None)       # never subscribed: no ValueError

    def test_l8_submachine_enter_still_clears_ctx(self):
        inner = Machine(states=["x", "y"], transitions=[("go", "x", "y")], initial="x")
        sub = SubMachine("proc", inner)
        inner.ctx["stale"] = 1
        inner.trigger("go")
        sub.enter({})                                # reset() clears ctx (dead clear removed)
        assert inner.ctx == {}
        assert inner.state == "x"


# ─── 1.6.0: Machine.lint() — dead edges ─────────────────────────────────────

class TestLintDeadEdges:
    """lint() dead_edge findings (spec §3.1): explicit/wildcard shadowing,
    internal edges, and the 'shadowed_by edge is never itself dead' invariant."""

    def test_e3_dead_edge_shadowed_explicit(self):
        m = Machine(states=["a", "b", "c"],
                    transitions=[("go", "a", "b"), ("go", "a", "c")],
                    initial="a")
        findings = m.lint()
        assert len(findings) == 2
        dead, unreach = findings

        assert dead.kind == "dead_edge"
        assert dead.severity == "warning"
        assert dead.trigger == "go"
        assert dead.source == "a"
        assert dead.dest == "c"
        assert dead.shadowed_by == ("go", "a", "b")
        assert dead.state is None

        assert unreach.kind == "unreachable_state"
        assert unreach.severity == "warning"
        assert unreach.state == "c"
        assert unreach.trigger is None
        assert unreach.source is None
        assert unreach.dest is None
        assert unreach.shadowed_by is None

    def test_e4_guarded_edge_does_not_shadow(self):
        m = Machine(states=["a", "b", "c"],
                    transitions=[("go", "a", "b", lambda ctx: True), ("go", "a", "c")],
                    initial="a")
        assert m.lint() == []                    # guard is opaque: no shadowing, c reachable

    def test_e5_wildcard_dead_shadowed_by_every_state_explicit(self):
        m = Machine(states=["a", "b"],
                    transitions=[("t", "a", "b"), ("t", "b", "a"), ("t", "*", "a")],
                    initial="a")
        findings = m.lint()
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == "dead_edge"
        assert f.trigger == "t"
        assert f.source == "*"
        assert f.dest == "a"
        assert f.shadowed_by is None              # case (b): no single shadowing edge
        assert not any(x.kind == "unreachable_state" for x in findings)
        # No sinks either: a and b exit to each other, so include_info adds nothing.
        assert m.lint(include_info=True) == findings

    def test_e7_wildcard_dead_shadowed_by_earlier_wildcard(self):
        m = Machine(states=["a", "b"],
                    transitions=[("t", "*", "a"), ("t", "*", "b")],
                    initial="a")
        findings = m.lint()
        assert len(findings) == 2
        dead, unreach = findings
        assert dead.kind == "dead_edge"
        assert dead.trigger == "t"
        assert dead.source == "*"
        assert dead.dest == "b"
        assert dead.shadowed_by == ("t", "*", "a")
        assert unreach.kind == "unreachable_state"
        assert unreach.state == "b"

        findings_info = m.lint(include_info=True)
        assert len(findings_info) == 3
        dead2, unreach2, sink = findings_info
        assert dead2.kind == "dead_edge"
        assert dead2.dest == "b"
        assert unreach2.kind == "unreachable_state"
        assert unreach2.state == "b"
        assert sink.kind == "sink_state"
        assert sink.state == "a"                  # self-loop only, rescued nothing

    def test_e8a_internal_edge_shadows_following_explicit(self):
        m = Machine(states=["a", "b"],
                    transitions=[("t", "a", None), ("t", "a", "b")],
                    initial="a")
        findings = m.lint()
        assert len(findings) == 2
        dead, unreach = findings
        assert dead.kind == "dead_edge"
        assert dead.trigger == "t"
        assert dead.source == "a"
        assert dead.dest == "b"
        assert dead.shadowed_by == ("t", "a", None)
        assert unreach.kind == "unreachable_state"
        assert unreach.state == "b"

    def test_e8b_internal_edge_itself_dead(self):
        m = Machine(states=["a", "b"],
                    transitions=[("t", "a", "b"), ("t", "a", None)],
                    initial="a")
        findings = m.lint()
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == "dead_edge"
        assert f.trigger == "t"
        assert f.source == "a"
        assert f.dest is None
        assert f.shadowed_by == ("t", "a", "b")

    def test_duplicate_identical_edges_second_is_dead(self):
        m = Machine(states=["a", "b"],
                    transitions=[("t", "a", "b"), ("t", "a", "b")],
                    initial="a")
        findings = m.lint()
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == "dead_edge"
        assert f.trigger == "t"
        assert f.source == "a"
        assert f.dest == "b"
        assert f.shadowed_by == ("t", "a", "b")   # shadowed by the first (identical) edge

    def test_shadowed_by_edge_is_never_itself_dead_three_edges(self):
        # Three unguarded edges on the same (trigger, source): only the first
        # is live. shadowed_by must always cite the first, never the nearest,
        # and the cited edge itself must never appear among the dead ones.
        m = Machine(states=["a", "b", "c", "d"],
                    transitions=[("t", "a", "b"), ("t", "a", "c"), ("t", "a", "d")],
                    initial="a")
        dead_edges = [f for f in m.lint() if f.kind == "dead_edge"]
        assert len(dead_edges) == 2
        for f in dead_edges:
            assert f.shadowed_by == ("t", "a", "b")
        dead_signatures = {(f.trigger, f.source, f.dest) for f in dead_edges}
        assert ("t", "a", "b") not in dead_signatures


# ─── 1.6.0: Machine.lint() — reachability ───────────────────────────────────

class TestLintReachability:
    """lint() unreachable_state findings (spec §3.2): BFS over disparable
    edges only, with per-state (not global) wildcard shadowing."""

    def test_e2_unreachable_state_and_sink_dedup(self):
        m = Machine(states=["a", "b", "c"], transitions=[("go", "a", "b")], initial="a")
        findings = m.lint()
        assert len(findings) == 1
        assert findings[0].kind == "unreachable_state"
        assert findings[0].state == "c"

        findings_info = m.lint(include_info=True)
        assert len(findings_info) == 2
        unreach, sink = findings_info
        assert unreach.kind == "unreachable_state"
        assert unreach.state == "c"
        assert sink.kind == "sink_state"
        assert sink.state == "b"                  # no sink for c: dedup against unreachable

    def test_e6_wildcard_reaches_further_state(self):
        m = Machine(states=["a", "b", "c"],
                    transitions=[("t", "a", "b"), ("t", "*", "c")],
                    initial="a")
        assert m.lint() == []                     # wildcard alive from b and c
        findings = m.lint(include_info=True)
        assert len(findings) == 1
        assert findings[0].kind == "sink_state"
        assert findings[0].state == "c"

    def test_e7bis_wildcard_locally_shadowed_not_globally_dead(self):
        m = Machine(states=["a", "c"],
                    transitions=[("t", "a", "a"), ("t", "*", "c")],
                    initial="a")
        findings = m.lint()
        assert len(findings) == 1
        assert findings[0].kind == "unreachable_state"
        assert findings[0].state == "c"
        assert not any(f.kind == "dead_edge" for f in findings)  # wildcard alive from c


# ─── 1.6.0: Machine.lint() — sinks ──────────────────────────────────────────

class TestLintSinks:
    """lint() sink_state findings (spec §3.3): info-only, reachable states
    with no disparable escape edge; deduped against unreachable_state."""

    def test_e1_clean_order_machine_only_sink_with_info(self):
        m = make_order()
        assert m.lint() == []
        findings = m.lint(include_info=True)
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == "sink_state"
        assert f.severity == "info"
        assert f.state == "approved"
        assert f.trigger is None
        assert f.source is None
        assert f.dest is None
        assert f.shadowed_by is None

    def test_e9_self_loop_only_is_sink_with_info(self):
        m = Machine(states=["x"], transitions=[("spin", "x", "x")], initial="x")
        assert m.lint() == []
        findings = m.lint(include_info=True)
        assert len(findings) == 1
        assert findings[0].kind == "sink_state"
        assert findings[0].state == "x"

    def test_no_transitions_machine_only_initial_is_sink(self):
        m = Machine(states=["a"], transitions=[], initial="a")
        assert m.lint() == []
        findings = m.lint(include_info=True)
        assert len(findings) == 1
        assert findings[0].kind == "sink_state"
        assert findings[0].state == "a"


# ─── 1.6.0: Machine.lint() — contract ───────────────────────────────────────

class TestLintContract:
    """lint() cross-cutting contract: purity, determinism, ordering, __all__
    export, frozen findings, include_info default, and the E12 demo machine."""

    def test_e10_lint_is_pure(self):
        m = make_order()
        m.trigger("submit")
        m.trigger("approve", score=80)
        snapshot = make_order()
        snapshot.trigger("submit")
        snapshot.trigger("approve", score=80)
        assert m == snapshot

        before_state, before_ctx = m.state, dict(m.ctx)
        before_history = list(m.history)
        m.lint(include_info=True)
        assert m.state == before_state
        assert m.ctx == before_ctx
        assert m.history == before_history
        assert m == snapshot                      # still value-equal to the untouched copy

    def test_two_consecutive_calls_return_equal_lists(self):
        m = Machine(states=["a", "b"],
                    transitions=[("t", "*", "a"), ("t", "*", "b")],
                    initial="a")
        assert m.lint(include_info=True) == m.lint(include_info=True)

    def test_ordering_dead_edge_then_unreachable_then_sink_alphabetical(self):
        # Registration order is s0, zeta, alpha but unreachable/sink findings
        # must sort by state name, not registration order (spec §4).
        m = Machine(states=["s0", "zeta", "alpha"], transitions=[], initial="s0")
        findings = m.lint(include_info=True)
        assert len(findings) == 3
        first, second, third = findings
        assert first.kind == "unreachable_state"
        assert first.state == "alpha"
        assert second.kind == "unreachable_state"
        assert second.state == "zeta"
        assert third.kind == "sink_state"
        assert third.state == "s0"

    def test_e11_lintfinding_exported_in_all(self):
        import tramoya
        assert "LintFinding" in tramoya.__all__

    def test_e11_lintfinding_raises_frozen_instance_error_on_mutation(self):
        import dataclasses
        f = LintFinding(kind="dead_edge", severity="warning", message="unused")
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.kind = "sink_state"

    def test_e12_demo_machine_wildcard_rescues_from_sink(self):
        # Reconstruction (spec E12) of the tramoya.py __main__ demo: the order
        # machine plus a cancel wildcard to rejected and a log wildcard
        # internal transition — 6 transitions total, nothing left dangling.
        m = Machine(
            states=["draft", "submitted", "approved", "rejected"],
            transitions=[
                ("submit",  "draft",     "submitted"),
                ("approve", "submitted", "approved", lambda ctx: ctx.get("score", 0) > 50),
                ("reject",  "submitted", "rejected"),
                ("revise",  "rejected",  "draft"),
                ("cancel",  "*",         "rejected"),
                ("log",     "*",         None),
            ],
            initial="draft",
        )
        assert m.lint(include_info=True) == []

    def test_include_info_default_false_hides_info_keeps_warnings(self):
        m = Machine(states=["a", "b", "c"], transitions=[("go", "a", "b")], initial="a")
        default = m.lint()
        assert len(default) == 1
        assert all(f.severity == "warning" for f in default)

        with_info = m.lint(include_info=True)
        assert len(with_info) == 2
        assert any(f.severity == "info" for f in with_info)


# ─── 1.6.0: audit 2026-10 (forja lot) — re-entrancy and to_dict snapshots ──

class TestReentrancy:
    """Re-entrant trigger()/reset() from inside a callback is unsupported
    (DeprecationWarning since 1.6.0, raises in 2.0), but history/undo must
    stay consistent in the common auto-transition-on-enter case."""

    @staticmethod
    def _auto(fail=False, **kw):
        h = {}
        def enter_b(ctx):
            h["m"].trigger("go2")
            if fail:
                raise RuntimeError("boom")
        m = Machine(states=["a", "b", "c"], transitions=[("go1", "a", "b"), ("go2", "b", "c")],
                    initial="a", on_enter={"b": enter_b}, **kw)
        h["m"] = m
        return m

    def test_nested_trigger_warns_and_history_stays_chronological(self):
        m = self._auto()
        with pytest.warns(DeprecationWarning, match="re-entrant"):
            m.trigger("go1")
        assert m.state == "c"
        assert m.history == ["a", "b"]          # was ['b', 'a']: undo() moved forward
        assert m.undo() == "b"
        assert m.undo() == "a"

    def test_nested_commit_then_outer_failure_leaves_no_phantom_history(self):
        m = self._auto(fail=True)
        with pytest.warns(DeprecationWarning), pytest.raises(RuntimeError):
            m.trigger("go1")
        assert m.state == "a"
        assert m.history == []                  # was ['b']: undo() landed on a never-observed state
        with pytest.raises(MachineError):
            m.undo()

    def test_nested_commit_then_failure_at_maxlen_leaves_no_phantom(self):
        h = {}
        def enter_b(ctx):
            h["m"].trigger("go2")
            raise RuntimeError("boom")
        m = Machine(["z", "a", "b", "c"], [("z2a", "z", "a"), ("go1", "a", "b"), ("go2", "b", "c")],
                    "z", history_size=1, on_enter={"b": enter_b})
        h["m"] = m
        m.trigger("z2a")
        with pytest.warns(DeprecationWarning), pytest.raises(RuntimeError):
            m.trigger("go1")
        assert m.state == "a"
        # The nested commit evicted the real 'z' entry at maxlen (deque semantics,
        # unrecoverable); what must never survive is the phantom 'b'.
        assert "b" not in m.history
        assert len(m.history) <= 1

    def test_reentrant_from_action_warns(self):
        h = {}
        m = Machine(["A", "B", "X"],
                    [("go", "A", "B", None, lambda c: h["m"].trigger("side")), ("side", "A", "X")], "A")
        h["m"] = m
        with pytest.warns(DeprecationWarning, match="re-entrant"):
            m.trigger("go")

    def test_reset_inside_callback_warns(self):
        h = {}
        m = Machine(["A", "B"], [("go", "A", "B")], "A", on_enter={"B": lambda c: h["m"].reset()})
        h["m"] = m
        with pytest.warns(DeprecationWarning, match="re-entrant"):
            m.trigger("go")

    def test_observer_chaining_is_not_reentrant(self):
        # Observers run after commit: chaining from an observer is sequential.
        m = Machine(["a", "b", "c"], [("go1", "a", "b"), ("go2", "b", "c")], "a")
        m.subscribe(lambda t, s, d, c: m.trigger("go2") if t == "go1" else None)
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            m.trigger("go1")
        assert m.state == "c"
        assert m.history == ["a", "b"]

    def test_non_reentrant_code_emits_no_warning(self):
        m = make_order()
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            m.trigger("submit")
            m.trigger("approve", score=80)
            m.undo()
            m.transition_to("approved", score=80)
            m.reset()


class TestToDictSnapshots:
    """to_dict() returns a real snapshot: deep copies (shallow with
    shallow_ctx=True), never live references to ctx or the undo history."""

    def test_snapshot_does_not_change_when_action_mutates_nested_value(self):
        m = Machine(["a", "b"], [("go", "a", "b", None, lambda c: c["items"].append(1))],
                    "a", ctx={"items": []})
        snap = m.to_dict()
        m.trigger("go")
        assert snap["ctx"]["items"] == []
        m2 = Machine(["a", "b"], [], "a")
        m2.load_dict(snap)
        assert m2.ctx["items"] == []

    def test_mutating_to_dict_output_does_not_corrupt_live_ctx_or_undo(self):
        m = Machine(["a", "b"], [("go", "a", "b")], "a", ctx={"items": []})
        m.trigger("go")
        d = m.to_dict()
        d["ctx"]["items"].append(8)
        d["history"][0]["ctx"]["items"].append(9)
        assert m.ctx == {"items": []}
        m.undo()
        assert m.ctx == {"items": []}

    def test_shallow_ctx_opt_out_keeps_to_dict_shallow(self):
        shared = []
        m = Machine(["a"], [], "a", ctx={"items": shared}, shallow_ctx=True)
        assert m.to_dict()["ctx"]["items"] is shared

    def test_submachine_restore_restores_exactly_what_exit_saved(self):
        inner = Machine(["a", "b"], [("go", "a", "b", None, lambda c: c["items"].append(1))],
                        "a", ctx={"items": []})
        sub = SubMachine("p", inner)
        sub.exit({})
        inner.trigger("go")
        sub.restore()
        assert inner.state == "a"
        assert inner.ctx["items"] == []


# ─── Audit 2026-10: motor y API ──────────────────────────────────────────────

class TestAudit2026_10_Motor:
    """Regression anchors for the 2026-10 adversarial audit (engine/API batch)."""

    # PER-MOT-005: explicit-vs-wildcard classification in transition_to()
    def test_transition_to_trigger_with_explicit_edge_is_explicit_even_if_wildcard_first(self):
        log = []
        m = Machine(
            ["a", "d"],
            [
                ("t1", "*", "d", None, lambda c: log.append("t1-wild")),
                ("t1", "a", "d", None, lambda c: log.append("t1-explicit")),
                ("t2", "a", "d", None, lambda c: log.append("t2")),
            ],
            "a",
        )
        m.transition_to("d")
        assert log == ["t1-explicit"]

    def test_transition_to_explicit_registered_before_wildcard_control(self):
        log = []
        m = Machine(
            ["a", "d"],
            [
                ("t1", "a", "d", None, lambda c: log.append("t1-explicit")),
                ("t1", "*", "d", None, lambda c: log.append("t1-wild")),
                ("t2", "a", "d", None, lambda c: log.append("t2")),
            ],
            "a",
        )
        m.transition_to("d")
        assert log == ["t1-explicit"]

    # PER-MOT-004: no_deterministic_edge only if the winner PRECEDES an edge to dest
    def test_transition_to_guard_blocked_and_later_edge_elsewhere_is_guard_rejected(self):
        m = Machine(
            ["a", "b", "c"],
            [("go", "a", "b", lambda c: c.get("ok", False)), ("go", "a", "c")],
            "a",
        )
        with pytest.raises(GuardRejected) as ei:
            m.transition_to("b")
        assert ei.value.reason == "guard_rejected"
        assert m.state == "a"

    def test_transition_to_higher_priority_edge_elsewhere_is_no_deterministic_edge(self):
        m = Machine(
            ["a", "b", "c"],
            [("go", "a", "c"), ("go", "a", "b", lambda c: c.get("ok", False))],
            "a",
        )
        with pytest.raises(GuardRejected) as ei:
            m.transition_to("b")
        assert ei.value.reason == "no_deterministic_edge"

    def test_transition_to_guard_passing_still_commits(self):
        m = Machine(
            ["a", "b", "c"],
            [("go", "a", "b", lambda c: c.get("ok", False)), ("go", "a", "c")],
            "a",
        )
        assert m.transition_to("b", ok=True) == "b"

    # PER-SER-001: from_dict/from_json structural validation
    @pytest.mark.parametrize("js", [
        '{}', '[]', 'null', '"a"', '{"state":"a","initial":[1]}', '{"state":[]}',
    ])
    def test_from_json_wrong_structure_raises_machine_error(self, js):
        with pytest.raises(MachineError):
            Machine.from_json(["a", "b"], [("t", "a", "b")], js)

    def test_from_dict_non_dict_raises_machine_error(self):
        with pytest.raises(MachineError, match="expects a dict"):
            Machine.from_dict(["a", "b"], [("t", "a", "b")], ["state", "a"])  # deliberately wrong type

    def test_from_json_minimal_document_still_builds(self):
        m = Machine.from_json(["a", "b"], [("t", "a", "b")], '{"state":"a"}')
        assert m.state == "a"

    def test_from_json_malformed_json_still_raises_json_error(self):
        with pytest.raises(json.JSONDecodeError):
            Machine.from_json(["a", "b"], [("t", "a", "b")], "{not json")

    # PER-MOT-006: exceptions survive pickle / copy / deepcopy
    def test_invalid_transition_pickle_roundtrip(self):
        e = InvalidTransition("go", "a", reason="unknown_state")
        e2 = pickle.loads(pickle.dumps(e))
        assert type(e2) is InvalidTransition
        assert (e2.trigger, e2.state, e2.reason) == ("go", "a", "unknown_state")
        assert str(e2) == str(e)

    def test_guard_rejected_pickle_roundtrip(self):
        e = GuardRejected("go", "a", reason="no_deterministic_edge")
        e2 = pickle.loads(pickle.dumps(e))
        assert type(e2) is GuardRejected
        assert (e2.trigger, e2.state, e2.reason) == ("go", "a", "no_deterministic_edge")
        assert str(e2) == str(e)

    @pytest.mark.parametrize("clone", [copy.copy, copy.deepcopy])
    def test_exceptions_copy_and_deepcopy(self, clone):
        for e in (InvalidTransition("go", "a"), GuardRejected("go", "a")):
            e2 = clone(e)
            assert type(e2) is type(e)
            assert (e2.trigger, e2.state, e2.reason) == (e.trigger, e.state, e.reason)
            assert str(e2) == str(e)

    def test_exception_stored_in_ctx_does_not_break_next_trigger(self):
        m = Machine(["a", "b"], [("go", "a", "b")], "a")
        try:
            m.trigger("nope")
        except InvalidTransition as e:
            m.ctx["last_error"] = e
        assert m.trigger("go") == "b"
        assert isinstance(m.ctx["last_error"], InvalidTransition)

    # PER-LOG-001: registering the same _Transition instance twice
    def test_same_transition_instance_twice_is_one_dead_edge(self):
        from tramoya import _Transition
        tr = _Transition("t", "a", "b")
        m = Machine(["a", "b"], [tr, tr], "a")
        assert [f.kind for f in m.lint()].count("dead_edge") == 1
        assert m.trigger("t") == "b"

    def test_equal_but_distinct_instances_between_are_two_dead_edges(self):
        from tramoya import _Transition
        t = _Transition("t", "a", "b")
        u = _Transition("t", "a", "b")
        m = Machine(["a", "b"], [t, u, t], "a")
        assert [f.kind for f in m.lint()].count("dead_edge") == 2

    def test_same_wildcard_instance_twice_is_one_dead_edge(self):
        from tramoya import _Transition
        w = _Transition("t", "*", "a")
        m = Machine(["a", "b"], [w, w], "a")
        assert [f.kind for f in m.lint()].count("dead_edge") == 1

    # PER-MOT-012: "*" as a state name is reserved
    def test_wildcard_state_name_deprecated(self):
        with pytest.warns(DeprecationWarning, match="reserved"):
            Machine(["*", "b"], [], "b")

    def test_no_wildcard_state_no_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Machine(["a", "b"], [("go", "*", "b")], "a")

    # PER-CTX-001: an empty caller-supplied ctx dict is bound by reference
    def test_empty_ctx_dict_is_bound_by_reference(self):
        shared = {}
        m = Machine(["a", "b"], [("go", "a", "b")], "a", ctx=shared)
        m.trigger("go", x=1)
        assert shared is m.ctx
        assert shared == {"x": 1}

    # PER-MOT-011: early validation of callables and tuple length
    def test_non_callable_guard_rejected_at_construction(self):
        with pytest.raises(MachineError, match="guard"):
            Machine(["a", "b"], [("go", "a", "b", "yes")], "a")

    def test_non_callable_action_rejected_at_construction(self):
        with pytest.raises(MachineError, match="action"):
            Machine(["a", "b"], [("go", "a", "b", None, "yes")], "a")

    def test_non_callable_on_enter_on_exit_on_transition_rejected(self):
        with pytest.raises(MachineError, match=r"on_enter\['b'\] must be callable"):
            Machine(["a", "b"], [], "a", on_enter={"b": None})  # deliberately wrong type
        with pytest.raises(MachineError, match=r"on_exit\['a'\] must be callable"):
            Machine(["a", "b"], [], "a", on_exit={"a": 3})  # deliberately wrong type
        with pytest.raises(MachineError, match="on_transition"):
            Machine(["a", "b"], [], "a", on_transition="nope")  # deliberately wrong type

    def test_subscribe_non_callable_rejected_and_state_untouched(self):
        m = Machine(["a", "b"], [("go", "a", "b")], "a")
        with pytest.raises(MachineError, match="observer"):
            m.subscribe(None)  # deliberately wrong type
        assert m.trigger("go") == "b"

    def test_long_transition_tuple_warns_and_still_works(self):
        with pytest.warns(DeprecationWarning, match="extra elements"):
            m = Machine(["a", "b"], [("go", "a", "b", None, None, "extra", 7)], "a")
        assert m.trigger("go") == "b"

    def test_five_element_tuple_does_not_warn(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Machine(["a", "b"], [("go", "a", "b", None, lambda c: None)], "a")


class TestAudit2026_10_Compuestos:
    """Regression anchors for the 2026-10 adversarial audit (composites,
    serialization, builder, export and docs batch)."""

    # PER-CMP-006: a failed ParallelMachine.load_dict puts the SAME objects back
    def test_parallel_failed_load_keeps_lock_ctx_and_original_error(self):
        r1 = Machine(["a", "b"], [("t", "a", "b")], "a", shallow_ctx=True)
        lock = threading.Lock()
        r1.ctx["lock"] = lock
        r2 = Machine(["x", "y"], [("t", "x", "y")], "x")
        pm = ParallelMachine(r1=r1, r2=r2)
        with pytest.raises(MachineError, match="NOPE"):
            pm.load_dict({"r1": {"state": "b"}, "r2": {"state": "NOPE"}})
        assert r1.state == "a"
        assert r1.ctx["lock"] is lock

    def test_parallel_failed_load_restores_loaded_region_despite_untouched_uncopyable_one(self):
        b = Machine(["p", "q"], [], "p", shallow_ctx=True)
        b.ctx["lock"] = threading.Lock()  # never part of the load: must not matter
        a = Machine(["x", "y"], [("t", "x", "y")], "x")
        c = Machine(["m", "n"], [], "m")
        pm = ParallelMachine(b=b, a=a, c=c)
        with pytest.raises(MachineError, match="NOPE"):
            pm.load_dict({"a": {"state": "y"}, "c": {"state": "NOPE"}})
        assert a.state == "x"
        assert pm.state == {"b": "p", "a": "x", "c": "m"}

    def test_parallel_failed_load_preserves_ctx_object_identity(self):
        ref = [1, 2, 3]
        r1 = Machine(["a", "b"], [], "a", ctx={"k": ref})
        r2 = Machine(["x", "y"], [], "x")
        pm = ParallelMachine(r1=r1, r2=r2)
        with pytest.raises(MachineError):
            pm.load_dict({"r1": {"state": "b", "ctx": {}}, "r2": {"state": "NOPE"}})
        assert r1.state == "a"
        assert r1.ctx["k"] is ref

    def test_parallel_successful_load_still_applies_everything(self):
        r1 = Machine(["a", "b"], [], "a")
        r2 = Machine(["x", "y"], [], "x")
        pm = ParallelMachine(r1=r1, r2=r2)
        pm.load_dict({"r1": {"state": "b", "ctx": {"n": 1}}, "r2": {"state": "y"}})
        assert pm.state == {"r1": "b", "r2": "y"}
        assert r1.ctx == {"n": 1}

    # PER-SUB-001: load_dict / SubMachine honor shallow_ctx
    def test_load_dict_shallow_ctx_copies_shallowly_ctx_and_history(self):
        m = Machine(["a", "b"], [], "a", shallow_ctx=True)
        lock = threading.Lock()
        data = {"state": "b", "ctx": {"lock": lock},
                "history": [{"state": "a", "ctx": {"lock": lock}}]}
        m.load_dict(data)
        assert m.state == "b"
        assert m.ctx["lock"] is lock
        assert m.ctx is not data["ctx"]  # still a copy of the dict itself
        m.undo()
        assert m.state == "a"
        assert m.ctx["lock"] is lock

    def test_load_dict_default_mode_still_rejects_uncopyable_ctx(self):
        m = Machine(["a", "b"], [], "a")
        with pytest.raises(MachineError, match="not deep-copyable"):
            m.load_dict({"state": "b", "ctx": {"lock": threading.Lock()}})
        assert m.state == "a"

    def test_submachine_restore_with_shallow_inner_keeps_state_and_lock(self):
        inner = Machine(["s1", "s2"], [("n", "s1", "s2")], "s1", shallow_ctx=True)
        sub = SubMachine("p", inner)
        lock = threading.Lock()
        inner.ctx["lock"] = lock
        inner.trigger("n")
        sub.exit({})
        sub.restore()
        assert inner.state == "s2"
        assert inner.ctx["lock"] is lock

    def test_submachine_enter_shared_key_by_reference_when_inner_shallow(self):
        inner = Machine(["s1", "s2"], [], "s1", shallow_ctx=True)
        sub = SubMachine("p", inner, shared_keys=["lock"])
        lock = threading.Lock()
        sub.enter({"lock": lock})
        assert inner.ctx["lock"] is lock

    def test_submachine_enter_uncopyable_shared_key_with_default_inner_is_machine_error(self):
        inner = Machine(["s1", "s2"], [], "s1")
        sub = SubMachine("p", inner, shared_keys=["lock"])
        with pytest.raises(MachineError, match="shared key 'lock' is not deep-copyable"):
            sub.enter({"lock": threading.Lock()})

    def test_submachine_enter_default_inner_still_deep_copies_shared_keys(self):
        inner = Machine(["s1", "s2"], [], "s1")
        sub = SubMachine("p", inner, shared_keys=["cfg", "missing"])
        cfg = {"depth": [1, 2]}
        sub.enter({"cfg": cfg, "other": 1})
        assert inner.ctx == {"cfg": {"depth": [1, 2]}}
        assert inner.ctx["cfg"] is not cfg
        assert inner.ctx["cfg"]["depth"] is not cfg["depth"]

    # PER-CMP-004: ParallelMachine.trigger is not atomic across regions (documented)
    def test_parallel_trigger_keeps_earlier_regions_committed_when_a_later_one_raises(self):
        def boom(ctx):
            raise RuntimeError("boom")

        a = Machine(["x", "y"], [("go", "x", "y")], "x")
        b = Machine(["p", "q"], [("go", "p", "q")], "p", on_enter={"q": boom})
        pm = ParallelMachine(a=a, b=b)
        with pytest.raises(RuntimeError, match="boom"):
            pm.trigger("go")
        assert pm.state == {"a": "y", "b": "p"}

    # PER-CMP-005: guard-blocked vs. unknown trigger; undo(region="")
    def test_parallel_trigger_guard_blocked_in_every_region_is_guard_rejected(self):
        a = Machine(["a", "b"], [("go", "a", "b", lambda c: False)], "a")
        pm = ParallelMachine(r=a)
        with pytest.raises(GuardRejected) as exc:
            pm.trigger("go")
        assert exc.value.reason == "guard_rejected"
        assert exc.value.trigger == "go"
        assert pm.state == {"r": "a"}

    def test_parallel_trigger_guard_blocked_with_other_region_lacking_trigger_is_guard_rejected(self):
        blocked = Machine(["a", "b"], [("go", "a", "b", lambda c: False)], "a")
        other = Machine(["x", "y"], [("stop", "x", "y")], "x")
        pm = ParallelMachine(blocked=blocked, other=other)
        with pytest.raises(GuardRejected):
            pm.trigger("go")

    def test_parallel_trigger_unknown_everywhere_is_still_invalid_transition(self):
        a = Machine(["a", "b"], [("go", "a", "b", lambda c: False)], "a")
        pm = ParallelMachine(r=a)
        with pytest.raises(InvalidTransition) as exc:
            pm.trigger("nope")
        assert exc.value.reason == "no_edge"

    def test_parallel_undo_empty_region_name_is_unknown_region(self):
        a = Machine(["a", "b"], [("go", "a", "b")], "a")
        pm = ParallelMachine(r=a)
        pm.trigger("go")
        with pytest.raises(MachineError, match="Unknown region ''"):
            pm.undo(region="")
        assert pm.state == {"r": "b"}

    def test_parallel_undo_region_none_still_undoes_all_regions(self):
        a = Machine(["a", "b"], [("go", "a", "b")], "a")
        c = Machine(["x", "y"], [("go", "x", "y")], "x")
        pm = ParallelMachine(a=a, c=c)
        pm.trigger("go")
        assert pm.undo(region=None) == {"a": "a", "c": "x"}

    # PER-BLD-001: build() hands the machine its own copy of the hook dicts
    def test_builder_hooks_registered_after_build_do_not_leak_into_built_machine(self):
        log = []
        b = MachineBuilder("a")
        b.add_states("a", "b")
        b.transition("go", "a", "b")

        @b.enter("b")
        def first(ctx):
            log.append("first")

        @b.exit("a")
        def leaving_a(ctx):
            log.append("exit-a")

        m = b.build()

        @b.enter("b")
        def second(ctx):
            log.append("second")

        @b.exit("a")
        def leaving_a_again(ctx):
            log.append("exit-a-2")

        m.trigger("go")
        assert log == ["exit-a", "first"]

    # PER-BLD-002: re-registering a guard/action for the same edge is deprecated
    def test_builder_duplicate_guard_warns(self):
        b = MachineBuilder("a")
        b.add_states("a", "b")
        b.transition("go", "a", "b")

        @b.guard("go", "a", "b")
        def g1(ctx):
            return True

        with pytest.warns(DeprecationWarning, match="already registered") as rec:
            @b.guard("go", "a", "b")
            def g2(ctx):
                return False
        assert "2.0" in str(rec[0].message)
        assert rec[0].filename == __file__
        assert b.build().can("go") is False  # the last one wins (today)

    def test_builder_duplicate_action_warns(self):
        b = MachineBuilder("a")
        b.add_states("a", "b")
        b.transition("go", "a", "b")

        @b.on("go", "a", "b")
        def act1(ctx):
            pass

        with pytest.warns(DeprecationWarning, match="action for .* already registered"):
            @b.on("go", "a", "b")
            def act2(ctx):
                pass

    def test_builder_distinct_edges_do_not_warn(self):
        b = MachineBuilder("a")
        b.add_states("a", "b", "c")
        with warnings.catch_warnings():
            warnings.simplefilter("error")

            @b.guard("go", "a", "b")
            def g1(ctx):
                return True

            @b.guard("go", "a", "c")
            def g2(ctx):
                return True

            @b.on("go", "a", "b")
            def act(ctx):
                pass

    # PER-EQU-001: initial takes part in equality
    def test_machines_differing_only_in_initial_are_not_equal(self):
        a = Machine(["x", "y"], [], "x")
        b = Machine(["x", "y"], [], "y")
        b.reset("x")
        assert a.state == b.state
        assert a != b

    def test_machines_with_same_initial_are_equal(self):
        a = Machine(["x", "y"], [], "x")
        b = Machine(["x", "y"], [], "x")
        assert a == b

    # PER-SER-003: load_dict validates every history entry but copies only the kept ones
    def test_load_dict_history_size_zero_never_copies_dropped_history(self):
        m = Machine(["a", "b"], [], "a", history_size=0)
        hist = [{"state": "a", "ctx": {"lock": threading.Lock()}} for _ in range(3)]
        m.load_dict({"state": "b", "history": hist})
        assert m.state == "b"
        assert m.history == []

    def test_load_dict_truncated_history_never_copies_dropped_entries(self):
        m = Machine(["a", "b", "c"], [], "a", history_size=2)
        hist = [{"state": "a", "ctx": {"lock": threading.Lock()}} for _ in range(3)]
        hist += [{"state": "b", "ctx": {"n": 1}}, {"state": "c", "ctx": {"n": 2}}]
        with pytest.warns(DeprecationWarning, match="history truncated from 5 to 2"):
            m.load_dict({"state": "a", "history": hist})
        assert m.history == ["b", "c"]
        m.undo()
        assert m.ctx == {"n": 2}

    def test_load_dict_kept_history_is_still_deep_copied(self):
        m = Machine(["a", "b"], [], "a")
        entry_ctx = {"items": [1]}
        m.load_dict({"state": "b", "history": [{"state": "a", "ctx": entry_ctx}]})
        entry_ctx["items"].append(2)
        m.undo()
        assert m.ctx == {"items": [1]}

    def test_load_dict_still_validates_history_entries_that_will_be_dropped(self):
        m0 = Machine(["a", "b"], [], "a", history_size=0)
        with pytest.raises(MachineError, match="Unknown state 'zzz' in history"):
            m0.load_dict({"state": "b", "history": [{"state": "zzz"}]})
        assert m0.state == "a"
        m2 = Machine(["a", "b"], [], "a", history_size=2)
        hist = [{"state": "zzz"}, {"state": "a"}, {"state": "b"}]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with pytest.raises(MachineError, match="Unknown state 'zzz' in history"):
                m2.load_dict({"state": "b", "history": hist})
        assert m2.state == "a"
        with pytest.raises(MachineError, match="'ctx' must be a dict"):
            m0.load_dict({"state": "b", "history": [{"state": "a", "ctx": 3}]})

    # PER-CMP-003: SubMachine state is not part of the parent snapshot (documented, pinned)
    def test_submachine_state_is_not_in_parent_snapshot(self):
        def build():
            inner = Machine(["a", "b"], [("go", "a", "b")], "a")
            sub = SubMachine("p", inner)
            parent = Machine(
                ["idle", "p", "done"],
                [("start", "idle", "p"), ("finish", "p", "done")],
                "idle",
                on_enter={"p": sub.enter},
                on_exit={"p": sub.exit},
            )
            return parent, sub

        parent, sub = build()
        parent.trigger("start")
        sub.trigger("go")
        parent.trigger("finish")
        snap = parent.to_json()
        assert set(json.loads(snap)) == {"state", "initial", "ctx", "history"}

        parent2, sub2 = build()
        parent2.load_dict(json.loads(snap))
        assert parent2.state == "done"
        sub2.restore()
        assert sub2.machine.state == "a"  # inner back at its own initial state

    # PER-CMP-002: shared_keys flow parent -> child on enter() only
    def test_submachine_shared_keys_are_enter_only_and_restore_reloads_saved_inner_ctx(self):
        inner = Machine(["a", "b"], [("go", "a", "b")], "a")
        sub = SubMachine("p", inner, shared_keys=["user"])
        parent_ctx = {"user": "ana"}
        sub.enter(parent_ctx)
        assert inner.ctx == {"user": "ana"}
        inner.ctx["user"] = "inner-edit"
        inner.trigger("go")
        sub.exit(parent_ctx)
        assert parent_ctx == {"user": "ana"}  # nothing copied back on exit()
        parent_ctx["user"] = "changed-meanwhile"
        sub.restore()
        assert inner.state == "b"
        assert inner.ctx == {"user": "inner-edit"}  # exactly as saved at exit()

    # PER-EXP-001: every line separator is normalized before it can break a Mermaid edge
    @pytest.mark.parametrize("sep", ["\r", "\r\n", "\n", "\x0b", "\x0c", "\x85", "\u2028", "\u2029"])
    def test_mermaid_label_normalizes_line_separators(self, sep):
        m = Machine(["a", "b"], [(f"x{sep}b --> a : evil", "a", "b")], "a")
        out = m.to_mermaid()
        assert "\r" not in out
        assert "<br/>" in out
        assert len([ln for ln in out.splitlines() if "-->" in ln]) == 1
        assert len(out.splitlines()) == 2  # header + the single edge: no separator splits a line

    def test_mermaid_label_plain_trigger_unchanged(self):
        m = Machine(["a", "b"], [("go", "a", "b")], "a")
        assert m.to_mermaid() == "stateDiagram-v2\n    a --> b : go"


# ─── 1.6.0: audit 2026-10 — G4 follow-up (reviewer findings) ────────────────

class TestAudit2026_10_G4:
    """Gaps the blind G4 review found: the full-deque branch of
    _record_undo_point, warning attribution for undo()/transition_to(),
    to_dict() error type, tolerant validation, __reduce__ extras."""

    @staticmethod
    def _chain(prior, maxlen, nested=True):
        h = {}
        on_enter = {}
        if nested:
            on_enter = {"a": lambda c: h["m"].trigger("go1"), "b": lambda c: h["m"].trigger("go2")}
        m = Machine(["p", "a", "b", "c"],
                    [("warm", "p", "p"), ("start", "p", "a"), ("go1", "a", "b"), ("go2", "b", "c")],
                    "p", history_size=maxlen, on_enter=on_enter)
        h["m"] = m
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            for _ in range(prior):
                m.trigger("warm")
            m.trigger("start")
            if not nested:
                m.trigger("go1")
                m.trigger("go2")
        return m

    @pytest.mark.parametrize("maxlen", [1, 2, 3, 4, 6])
    @pytest.mark.parametrize("prior", [0, 1, 2, 3, 5])
    def test_nested_chain_matches_sequential_even_with_full_history(self, maxlen, prior):
        # Fills the deque to maxlen BEFORE the nested chain, so the popleft +
        # insert branch of _record_undo_point is exercised, then compares with
        # the plain sequential run: same state, same history, same undo walk.
        nested, flat = self._chain(prior, maxlen), self._chain(prior, maxlen, nested=False)
        assert nested.state == flat.state == "c"
        assert nested.history == flat.history
        assert len(nested.history) <= maxlen
        walk_n, walk_f = [], []
        for m, walk in ((nested, walk_n), (flat, walk_f)):
            while m.history:
                walk.append(m.undo())
        assert walk_n == walk_f

    def test_undo_inside_callback_warns_and_points_at_caller(self):
        h = {}
        m = Machine(["p", "a", "b"], [("start", "p", "a"), ("go", "a", "b")], "p",
                    on_enter={"b": lambda c: h["m"].undo()})
        h["m"] = m
        m.trigger("start")
        with pytest.warns(DeprecationWarning, match="re-entrant undo") as rec:
            m.trigger("go")
        assert rec[0].filename == __file__

    def test_transition_to_inside_callback_warns_and_points_at_caller(self):
        h = {}
        m = Machine(["a", "b", "c"], [("go1", "a", "b"), ("go2", "b", "c")], "a",
                    on_enter={"b": lambda c: h["m"].transition_to("c")})
        h["m"] = m
        with pytest.warns(DeprecationWarning, match="re-entrant trigger") as rec:
            m.trigger("go1")
        assert rec[0].filename == __file__
        assert m.state == "c"
        assert m.history == ["a", "b"]

    def test_reset_inside_callback_warning_points_at_caller(self):
        h = {}
        m = Machine(["A", "B"], [("go", "A", "B")], "A", on_enter={"B": lambda c: h["m"].reset()})
        h["m"] = m
        with pytest.warns(DeprecationWarning, match="re-entrant reset") as rec:
            m.trigger("go")
        assert rec[0].filename == __file__

    def test_to_dict_uncopyable_ctx_raises_machine_error_in_default_mode(self):
        m = Machine(["a"], [], "a", ctx={"lock": threading.Lock()})
        with pytest.raises(MachineError, match="deep-copyable"):
            m.to_dict()
        d = {}
        for _ in range(5000):
            d = {"k": d}
        m2 = Machine(["a"], [], "a", ctx={"deep": d})
        with pytest.raises(MachineError):
            m2.to_dict()

    def test_to_json_matches_to_dict_and_tolerates_what_to_dict_copies(self):
        m = Machine(["a", "b"], [("go", "a", "b")], "a", ctx={"items": [1, {"x": (1, 2)}]})
        m.trigger("go", n=2)
        assert json.loads(m.to_json()) == json.loads(json.dumps(m.to_dict()))

    def test_falsy_action_and_on_transition_placeholders_still_accepted(self):
        m = Machine(["a", "b"], [("go", "a", "b", None, False)], "a", on_transition=False)
        assert m.trigger("go") == "b"

    def test_non_callable_guard_in_transition_instance_rejected(self):
        with pytest.raises(MachineError, match="guard"):
            Machine(["a", "b"], [_Transition("go", "a", "b", guard=5)], "a")  # type: ignore[arg-type]
        with pytest.raises(MachineError, match="action"):
            Machine(["a", "b"], [_Transition("go", "a", "b", action="yes")], "a")  # type: ignore[arg-type]

    def test_reduce_keeps_extra_attributes(self):
        import pickle
        e = GuardRejected("go", "a")
        e.request_id = 7  # type: ignore[attr-defined]
        e2 = pickle.loads(pickle.dumps(e))
        assert (e2.trigger, e2.state, e2.reason, str(e2)) == ("go", "a", "guard_rejected", str(e))
        assert e2.request_id == 7  # type: ignore[attr-defined]
