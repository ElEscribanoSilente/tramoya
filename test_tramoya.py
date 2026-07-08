"""Tests for tramoya.py — comprehensive coverage."""

import json
import pytest
from tramoya import (
    Machine, MachineBuilder, SubMachine, ParallelMachine, WILDCARD,
    MachineError, InvalidTransition, GuardRejected,
)


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
        log = []
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
