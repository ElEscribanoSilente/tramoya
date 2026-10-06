"""
tramoya.py — The backstage machinery for your state machines. One file. Zero deps.

What tramoya does:
  ✓ Define machines with plain dicts or decorators (MachineBuilder)
  ✓ Guards (conditional transitions)
  ✓ Entry/exit actions per state
  ✓ Hierarchical states (SubMachine)
  ✓ Parallel states / orthogonal regions (ParallelMachine)
  ✓ Wildcard transitions ("*" source = any state)
  ✓ Internal transitions (dest=None, no exit/enter callbacks)
  ✓ History tracking with undo
  ✓ Observers (subscribe to all transitions)
  ✓ Serializable (runtime snapshot via JSON — topology must be reconstructed)
  ✓ Dot + Mermaid graph export
  ✓ Batch triggers (trigger_many)
  ✓ Imperative dispatch (transition_to(dest)) for non-event-driven integration
  ✓ Typed reason field on InvalidTransition / GuardRejected
  ✓ Opt-in shallow ctx snapshots (shallow_ctx=True) for hot paths
  ✓ Precomputed dispatch index — O(1) trigger lookup
  ✓ Static lint (unreachable states, dead edges, sink states)

Limitations:
  - Not thread-safe: no locking on state/ctx/history mutations
  - No async support: callbacks are synchronous only
  - SubMachine has no automatic done-state propagation to parent
  - SubMachine state is not part of the parent's to_dict()/load_dict() snapshot:
    persist the inner machine separately (planned for 1.8)
  - Not re-entrant: calling trigger()/transition_to()/undo()/reset() from inside
    a callback emits DeprecationWarning (since 1.6.0) and will raise in 2.0
  - ctx values must be deep-copyable in the default mode (use shallow_ctx=True
    for locks, sockets, handles)

Usage:
    from tramoya import Machine

    order = Machine(
        states=["draft", "submitted", "approved", "rejected"],
        transitions=[
            ("submit",  "draft",     "submitted"),
            ("approve", "submitted", "approved",  lambda ctx: ctx.get("score", 0) > 50),
            ("reject",  "submitted", "rejected"),
            ("revise",  "rejected",  "draft"),
            ("cancel",  "*",         "rejected"),   # wildcard: from any state
        ],
        initial="draft",
    )

    order.trigger("submit")               # draft → submitted
    order.trigger("approve", score=80)     # submitted → approved ✓
    print(order.state)                     # "approved"
    order.undo()                           # back to "submitted"

Decorator API:
    from tramoya import MachineBuilder

    b = MachineBuilder("idle")
    b.add_states("idle", "running", "paused", "done")

    @b.on("start", "idle", "running")
    def on_start(ctx):
        ctx["started_at"] = time.time()

    @b.guard("start", "idle", "running")
    def check_ready(ctx):
        return ctx.get("ready", False)

    machine = b.build()

License: MIT
Author: Independent — not affiliated with any framework.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import warnings
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Set, Tuple, Union

__version__ = "1.6.0"
__all__ = [
    "Machine", "LintFinding", "MachineBuilder", "SubMachine", "ParallelMachine", "WILDCARD",
    "MachineError", "InvalidTransition", "GuardRejected",
]

# Sentinel for wildcard source
WILDCARD = "*"

# ─── Exceptions ───────────────────────────────────────────────────────────────

class MachineError(Exception):
    pass

class InvalidTransition(MachineError):
    """No transition matches the trigger from the current state.

    Attributes:
        trigger: Trigger name that was attempted.
        state:   State the machine was in.
        reason:  One of "no_edge" (no transition registered), "unknown_state"
                 (raised by transition_to() when the destination state is not
                 declared). Defaults to "no_edge".
    """
    def __init__(self, trigger: str, state: str, reason: str = "no_edge"):
        self.trigger, self.state, self.reason = trigger, state, reason
        super().__init__(f"No transition '{trigger}' from state '{state}'")

    def __reduce__(self) -> Tuple[Any, ...]:
        # args holds only the message (so str() stays unchanged); rebuild via
        # the real signature so pickle/copy/deepcopy work, and carry __dict__ so
        # extra attributes set by callers survive the round-trip. (PER-MOT-006)
        return (type(self), (self.trigger, self.state, self.reason), self.__dict__)

class GuardRejected(MachineError):
    """All candidate transitions for the trigger were blocked by guards.

    Attributes:
        trigger: Trigger name that was attempted.
        state:   State the machine was in.
        reason:  "guard_rejected" from trigger(). transition_to() may instead
                 raise reason="no_deterministic_edge" when edges to the target
                 exist but a higher-priority edge shadows them. (L4)
    """
    def __init__(self, trigger: str, state: str, reason: str = "guard_rejected"):
        self.trigger, self.state, self.reason = trigger, state, reason
        super().__init__(f"Guard rejected '{trigger}' from '{state}'")

    def __reduce__(self) -> Tuple[Any, ...]:
        # args holds only the message (so str() stays unchanged); rebuild via
        # the real signature so pickle/copy/deepcopy work, and carry __dict__ so
        # extra attributes set by callers survive the round-trip. (PER-MOT-006)
        return (type(self), (self.trigger, self.state, self.reason), self.__dict__)


# ─── Transition ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Transition:
    trigger: str
    source: str                                                    # state name or "*" for wildcard
    dest: Optional[str]                                            # None = internal transition
    guard: Optional[Callable[[Mapping[str, Any]], bool]] = None    # receives read-only view
    action: Optional[Callable[[Dict[str, Any]], Any]] = None       # receives mutable ctx


@dataclass(frozen=True)
class LintFinding:
    """One static-analysis finding produced by Machine.lint().

    Attributes:
        kind:        "dead_edge" | "unreachable_state" | "sink_state".
        severity:    "warning" | "info".
        message:     Human-readable explanation. NOT a stable contract —
                     wording may change between versions; assert a loose
                     substring if you must, never exact equality.
        state:       Set for unreachable_state / sink_state.
        trigger:     Set for dead_edge.
        source:      Set for dead_edge. May be "*" (wildcard).
        dest:        Set for dead_edge. None means the shadowed edge is an
                     internal transition.
        shadowed_by: For dead_edge, the (trigger, source, dest) of the edge
                     that shadows this one. None when no single edge is
                     responsible (every state's own explicit edges do the
                     shadowing instead — see lint() docstring).
    """
    kind: str
    severity: str
    message: str
    state: Optional[str] = None
    trigger: Optional[str] = None
    source: Optional[str] = None
    dest: Optional[str] = None
    shadowed_by: Optional[Tuple[str, str, Optional[str]]] = None


_Runtime = Tuple[str, str, Dict[str, Any], Deque[Tuple[str, Dict[str, Any]]]]


# ─── Machine ──────────────────────────────────────────────────────────────────

class Machine:
    """
    Finite state machine. Minimal, correct, useful.

    Args:
        states:        List of state names.
        transitions:   List of (trigger, source, dest[, guard][, action]) tuples.
                       source="*" matches any state. dest=None for internal transitions.
        initial:       Starting state.
        on_enter:      Dict of {state: callback(ctx)} run when entering a state.
        on_exit:       Dict of {state: callback(ctx)} run when leaving a state.
        on_transition: Global callback(trigger, src, dst, ctx) on every transition.
        ctx:           Arbitrary context dict carried through the machine. The
                       machine takes ownership of this dict and mutates it in
                       place (trigger, undo, load_dict); never share one dict
                       between machines — pass `dict(template)` to each.
        history_size:  Max undo steps (0 = disabled).
        shallow_ctx:   If True, ctx snapshots use dict() instead of deepcopy().
                       10-50× faster for large/complex ctx, but mutable values
                       inside ctx (lists, dicts, custom objects) are NOT
                       protected — undo() and rollback-on-callback-failure will
                       see post-mutation state for nested values. Use only when
                       you know your actions don't mutate nested ctx values
                       in-place. Default False (full transactional safety).
    """

    def __init__(
        self,
        states: List[str],
        transitions: List[Union[Tuple[Any, ...], _Transition]],
        initial: str,
        on_enter: Optional[Dict[str, Callable[..., Any]]] = None,
        on_exit: Optional[Dict[str, Callable[..., Any]]] = None,
        on_transition: Optional[Callable[..., Any]] = None,
        ctx: Optional[Dict[str, Any]] = None,
        history_size: int = 50,
        shallow_ctx: bool = False,
    ):
        # Validate inputs
        for s in states:
            if not isinstance(s, str) or not s:
                raise MachineError(f"State names must be non-empty strings, got {s!r}")
            if s == WILDCARD:
                warnings.warn(
                    "state name '*' is reserved for wildcard transitions; "
                    "will raise MachineError in tramoya 2.0",
                    DeprecationWarning,
                    stacklevel=2,
                )  # (PER-MOT-012)
        if not isinstance(history_size, int) or history_size < 0:
            raise MachineError(f"history_size must be a non-negative integer, got {history_size!r}")

        self._states: Set[str] = set(states)
        self._transitions: Dict[str, List[_Transition]] = {}
        # Precomputed dispatch indices for O(1) lookup.
        # Built lazily by _add_transition; never mutated after __init__.
        self._dispatch: Dict[Tuple[str, str], List[_Transition]] = {}        # (source, trigger) -> [trans]
        self._wildcard_dispatch: Dict[str, List[_Transition]] = {}           # trigger -> [trans] (source="*")
        self._reverse_dispatch: Dict[str, List[Tuple[str, str]]] = {}        # dest -> [(source, trigger), ...]
        self._on_enter: Dict[str, Callable[..., Any]] = on_enter or {}
        self._on_exit: Dict[str, Callable[..., Any]] = on_exit or {}
        self._on_transition: Optional[Callable[..., Any]] = on_transition
        self._state: str = initial
        self._initial: str = initial
        self.ctx: Dict[str, Any] = ctx if ctx is not None else {}  # (PER-CTX-001)
        self._history: Deque[Tuple[str, Dict[str, Any]]] = deque(maxlen=history_size if history_size > 0 else None)
        self._history_size = history_size
        self._shallow_ctx = shallow_ctx
        self._observers: List[Callable[..., Any]] = []
        # Re-entrancy bookkeeping: _depth > 0 while a transition's callbacks
        # run; _pushes counts undo points recorded so far (net of undo()). It is
        # only ever read as a difference inside one _execute window, so reset()
        # and load_dict() need not adjust it.
        self._depth: int = 0
        self._pushes: int = 0

        if initial not in self._states:
            raise MachineError(f"Initial state '{initial}' not in states")

        for key, cb in self._on_enter.items():
            if key not in self._states:
                raise MachineError(f"on_enter references unknown state '{key}'")
            if not callable(cb):
                raise MachineError(f"on_enter['{key}'] must be callable")  # (PER-MOT-011)
        for key, cb in self._on_exit.items():
            if key not in self._states:
                raise MachineError(f"on_exit references unknown state '{key}'")
            if not callable(cb):
                raise MachineError(f"on_exit['{key}'] must be callable")  # (PER-MOT-011)
        if on_transition and not callable(on_transition):
            raise MachineError("on_transition must be callable")  # (PER-MOT-011)

        for t in transitions:
            self._add_transition(t)

    # ── Internal ──────────────────────────────────────────────────────────

    def _add_transition(self, t: Union[Tuple[Any, ...], _Transition]) -> None:
        if isinstance(t, _Transition):
            # Own copy: lint() keys on id(), so the SAME instance registered
            # twice must become two distinct edges. (PER-LOG-001)
            tr = dataclasses.replace(t)
            if tr.guard is not None and not callable(tr.guard):
                raise MachineError(f"guard for {tr.trigger!r} must be callable, got {tr.guard!r}")
            if tr.action and not callable(tr.action):
                raise MachineError(f"action for {tr.trigger!r} must be callable, got {tr.action!r}")
        else:
            if not isinstance(t, (tuple, list)) or len(t) < 3:
                raise MachineError(
                    f"Transition must be a tuple of (trigger, source, dest[, guard][, action]), "
                    f"got {t!r} (length {len(t) if isinstance(t, (tuple, list)) else 'N/A'})"
                )
            trigger, src, dst = t[0], t[1], t[2]
            guard = t[3] if len(t) > 3 else None
            action = t[4] if len(t) > 4 else None
            # Fail at construction, not at first dispatch. (PER-MOT-011)
            if guard is not None and not callable(guard):
                raise MachineError(f"guard for {trigger!r} must be callable, got {guard!r}")
            if action and not callable(action):  # falsy placeholders stay ignored, as before
                raise MachineError(f"action for {trigger!r} must be callable, got {action!r}")
            if len(t) > 5:
                warnings.warn(
                    f"transition tuple for {t[0]!r} has {len(t)} elements; extra elements "
                    f"are ignored and will raise MachineError in tramoya 2.0",
                    DeprecationWarning,
                    stacklevel=3,
                )
            tr = _Transition(trigger, src, dst, guard, action)

        # Validate: dest must exist (unless internal), source must exist or be wildcard
        if tr.dest is not None and tr.dest not in self._states:
            raise MachineError(f"State '{tr.dest}' not declared")
        if tr.source != WILDCARD and tr.source not in self._states:
            raise MachineError(f"State '{tr.source}' not declared")

        self._transitions.setdefault(tr.trigger, []).append(tr)

        # Populate dispatch indices. Explicit and wildcard live in separate
        # dicts so _match_candidates can preserve "explicit beats wildcard"
        # ordering with two O(1) lookups instead of a scan.
        if tr.source == WILDCARD:
            self._wildcard_dispatch.setdefault(tr.trigger, []).append(tr)
        else:
            self._dispatch.setdefault((tr.source, tr.trigger), []).append(tr)

        # Reverse index: dest -> [(source, trigger), ...]. Used by transition_to().
        # Internal transitions (dest=None) are excluded — they're not "going anywhere".
        if tr.dest is not None:
            self._reverse_dispatch.setdefault(tr.dest, []).append((tr.source, tr.trigger))

    def _match_candidates(self, name: str) -> List[_Transition]:
        """Get transitions matching trigger name from current state.
        Explicit source matches come first (in registration order),
        then wildcard matches. This guarantees explicit > wildcard priority."""
        explicit = self._dispatch.get((self._state, name), ())
        wildcard = self._wildcard_dispatch.get(name, ())
        if not explicit:
            return list(wildcard)
        if not wildcard:
            return list(explicit)
        return [*explicit, *wildcard]

    def _push_history(self, state: str, ctx_snapshot: Dict[str, Any]) -> None:
        if self._history_size > 0:
            self._history.append((state, ctx_snapshot))
            self._pushes += 1

    def _notify(self, trigger: str, src: str, dst: str, ctx: Dict[str, Any]) -> None:
        if self._on_transition:
            self._on_transition(trigger, src, dst, ctx)
        # Iterate a snapshot so an observer that subscribes/unsubscribes during
        # notification can't corrupt the iteration (skip or double-fire). (M8)
        for obs in list(self._observers):
            obs(trigger, src, dst, ctx)

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def initial(self) -> str:
        return self._initial

    @property
    def states(self) -> Set[str]:
        return set(self._states)

    @property
    def history(self) -> List[str]:
        """Previous state names (most recent last)."""
        return [state for state, _ctx in self._history]

    @property
    def available_triggers(self) -> List[str]:
        """Triggers valid from current state (ignores guards).

        Iteration order is registration order of triggers (first time each
        trigger name was seen in the transitions list passed to __init__)."""
        state = self._state
        return [
            trigger for trigger in self._transitions
            if (state, trigger) in self._dispatch or trigger in self._wildcard_dispatch
        ]

    @property
    def is_final(self) -> bool:
        """True if no triggers can fire from current state (dead end).
        Ignores guards — use is_stuck() for guard-aware check."""
        return len(self.available_triggers) == 0

    def is_stuck(self, **kwargs: Any) -> bool:
        """True if no trigger can actually fire from current state (evaluates guards).
        Unlike is_final, this checks if guards would block all transitions."""
        for trigger in self.available_triggers:
            if self.can(trigger, **kwargs):
                return False
        return True

    def can(self, trigger: str, **kwargs: Any) -> bool:
        """Check if trigger can fire (evaluates guards).
        Guards receive a read-only view of ctx to prevent accidental
        mutation. The view is shallow: top-level assignment raises, but a
        guard that mutates a nested value in place (ctx["items"].append)
        does reach the live ctx — keep guards pure."""
        frozen = MappingProxyType({**self.ctx, **kwargs})
        for tr in self._match_candidates(trigger):
            if tr.guard is None or tr.guard(frozen):
                return True
        return False

    def trigger(self, name: str, **kwargs: Any) -> str:
        """
        Fire a trigger. Returns the new state.
        Raises InvalidTransition or GuardRejected.

        Guard evaluation uses a frozen (read-only) view of ctx+kwargs; the
        view is shallow, so a guard that mutates a nested value in place is
        not stopped (keep guards pure). Context is only mutated after a guard
        passes. If any callback (on_exit, action, on_enter) raises, state,
        ctx and history roll back to their pre-call values (with
        shallow_ctx=True, nested mutable ctx values are not restored — see
        Machine.__init__). The rollback covers the machine, not the world:
        side effects of callbacks that already ran (e.g. on_exit of the old
        state) are not compensated, and after a rollback or undo() the ctx
        values are the snapshot's deep copies, not the original objects. In
        the default mode every ctx value must therefore be deep-copyable (a
        Lock or socket in ctx makes trigger() raise TypeError; use
        shallow_ctx=True for such values). (L2)

        Calling trigger()/transition_to()/undo()/reset() on this machine from
        inside one of its callbacks (re-entrancy) is unsupported: it emits
        DeprecationWarning since 1.6.0 and will raise MachineError in 2.0.
        For the common auto-transition-on-enter pattern, history stays
        chronological and a failing outer callback discards the undo points
        the nested commits recorded (entries those commits evicted at
        history_size are gone for good, as with any eviction); re-entering
        from on_exit/action, or calling reset()/undo() from a callback, is
        best-effort only.

        Internal transitions (dest=None) apply kwargs to ctx and run the action
        but record no undo point: their ctx changes are not reverted by a later
        undo(). (L1)

        The keyword names `name` (trigger), `trigger` (can) and `dest`
        (transition_to) collide with the positional parameter and raise
        TypeError; use other ctx keys. Positional-only parameters are planned
        for 2.0. (PER-MOT-010)
        """
        frozen = MappingProxyType({**self.ctx, **kwargs})

        candidates = self._match_candidates(name)
        if not candidates:
            raise InvalidTransition(name, self._state)

        for tr in candidates:
            if tr.guard is not None and not tr.guard(frozen):
                continue
            return self._execute(tr, name, kwargs)

        raise GuardRejected(name, self._state)

    def _snapshot_copy(self, ctx: Dict[str, Any], what: str) -> Dict[str, Any]:
        """Copy of a ctx dict for to_dict(): dict() with shallow_ctx=True,
        deepcopy otherwise, wrapping copy failures in MachineError (like
        load_dict) instead of leaking TypeError/RecursionError."""
        return dict(ctx) if self._shallow_ctx else self._deepcopy_ctx(ctx, what)

    def _runtime_snapshot(self) -> _Runtime:
        """Identity-preserving capture of (state, initial, ctx, history) for
        internal restore paths. No nested copies: the point is to put the SAME
        objects back, so it cannot fail on un-deep-copyable ctx values and
        external references into ctx stay valid."""
        return (self._state, self._initial, dict(self.ctx),
                deque(self._history, maxlen=self._history.maxlen))

    def _runtime_restore(self, snap: _Runtime) -> None:
        state, initial, ctx, history = snap
        self._state = state
        self._initial = initial
        self.ctx.clear()
        self.ctx.update(ctx)
        self._history = history

    def _warn_reentrant(self, what: str, stacklevel: int = 4) -> None:
        # stacklevel 4 = user callback -> trigger()/transition_to() -> _execute
        # -> here; undo()/reset() are one frame shallower and pass 3.
        warnings.warn(
            f"re-entrant {what} from inside a tramoya callback is unsupported: "
            f"history/undo and exit/enter ordering are best-effort only; "
            f"will raise MachineError in tramoya 2.0",
            DeprecationWarning,
            stacklevel=stacklevel,
        )

    def _execute(self, tr: _Transition, name: str, kwargs: Dict[str, Any]) -> str:
        """Run an already-selected transition whose guard has already passed.

        Shared by trigger() and transition_to() so a transition dispatches with
        exactly one guard evaluation. transition_to() previously delegated back
        to trigger(), which re-evaluated the guards; a non-pure guard could then
        return differently on the second pass and silently land in the wrong
        state. (M10)

        _depth tracks nesting: a callback that triggers this same machine is
        re-entrant (warned; see trigger()). Observers run after the window
        closes, so chaining a transition from an observer is sequential."""
        if self._depth > 0:
            self._warn_reentrant("trigger()")
        self._depth += 1
        try:
            old_state, dst = self._commit(tr, kwargs)
        finally:
            self._depth -= 1
        # Notify outside any try block — the transition is committed, observer
        # failures must not trigger rollback.
        self._notify(name, old_state, dst, self.ctx)
        return self._state

    def _commit(self, tr: _Transition, kwargs: Dict[str, Any]) -> Tuple[str, str]:
        """Apply `tr` with full rollback on any raise. Returns (old_state, dst)
        for the notification; dst == old_state for internal transitions."""
        # Snapshot before mutation, then commit.
        # shallow_ctx=True swaps deepcopy for dict() — 10-50× cheaper but
        # nested mutable values are aliased (see Machine.__init__ docstring).
        old_state = self._state
        old_ctx = dict(self.ctx) if self._shallow_ctx else copy.deepcopy(self.ctx)
        self.ctx.update(kwargs)
        marker = self._pushes  # undo points past this mark belong to nested commits

        # Internal transition: action only, no state change, no enter/exit
        if tr.dest is None:
            try:
                if tr.action:
                    tr.action(self.ctx)
            except BaseException:
                # Roll back on ANY raise (incl. KeyboardInterrupt/SystemExit),
                # then re-raise — honors "any callback raises → full rollback"
                # without swallowing the exception. (L3)
                self._rollback(old_state, old_ctx, marker)
                raise
            return old_state, old_state

        # Full transition — rollback on failure
        try:
            if old_state in self._on_exit:
                self._on_exit[old_state](self.ctx)

            if tr.action:
                tr.action(self.ctx)

            self._state = tr.dest

            if tr.dest in self._on_enter:
                self._on_enter[tr.dest](self.ctx)
        except BaseException:  # roll back on ANY raise, then re-raise (L3)
            self._rollback(old_state, old_ctx, marker)
            raise

        # Transition fully succeeded (incl. on_enter) — only now record the undo
        # point. Pushing before on_enter corrupted history when the deque was at
        # maxlen: append() evicts the oldest entry (len unchanged), so the
        # len-comparison pop never fired on rollback. (A1)
        if self._pushes == marker or self._history_size == 0:
            self._push_history(old_state, old_ctx)  # hot path: no nested commits
        else:
            self._record_undo_point(old_state, old_ctx, marker)
        return old_state, tr.dest

    def _rollback(self, old_state: str, old_ctx: Dict[str, Any], marker: int) -> None:
        self._state = old_state
        self.ctx.clear()
        self.ctx.update(old_ctx)
        # Undo points recorded by nested (re-entrant) commits inside the failed
        # callback describe a transition that no longer happened: drop them.
        # Pop by count, never by length — at maxlen, append() evicts instead
        # of growing, so lengths lie.
        for _ in range(self._pushes - marker):
            if self._history:
                self._history.pop()
                self._pushes -= 1

    def _record_undo_point(self, old_state: str, old_ctx: Dict[str, Any], marker: int) -> None:
        nested = self._pushes - marker
        if nested <= 0 or self._history_size == 0:
            self._push_history(old_state, old_ctx)
            return
        # Re-entrant commits during on_enter already recorded their undo
        # points. Insert ours *before* them so history stays chronological
        # (A→B→C reads ['A','B'], not ['B','A']) and undo() walks backwards.
        history = self._history
        if history.maxlen is not None and len(history) >= history.maxlen:
            if nested >= len(history):
                return  # ours would be the oldest entry: evicted on arrival
            history.popleft()  # make room the way append() would
        history.insert(len(history) - min(nested, len(history)), (old_state, old_ctx))
        self._pushes += 1

    def trigger_many(self, *triggers: Union[str, Tuple[str, Dict[str, Any]]]) -> str:
        """
        Fire multiple triggers in sequence. Returns final state.
        Each element is either a trigger name or (name, kwargs) tuple.
        NOT atomic: stops and raises on first failure, leaving the machine in
        the state reached by the last successful trigger — and ctx carrying the
        kwargs merged by every step that already ran (including internal steps,
        which leave no undo point). (L9)
        """
        for t in triggers:
            if isinstance(t, str):
                self.trigger(t)
            else:
                self.trigger(t[0], **t[1])
        return self._state

    def transition_to(self, dest: str, **kwargs: Any) -> str:
        """Move to `dest` by firing whichever registered trigger leads there.

        Useful when integrating with code that wants to set state imperatively
        (`obj.state = NEW`) rather than event-driven.

        Resolution order: candidate triggers (those with an edge to `dest` from
        the current state, or from "*") are tried in registration order,
        explicit sources before wildcards. For each candidate, we predict what
        trigger() would actually fire: if the first guard-passing edge of that
        trigger lands in `dest`, we commit. If it lands elsewhere (because
        another transition with the same trigger name has higher priority), we
        skip and try the next candidate. This avoids silently firing the wrong
        edge when a trigger name is overloaded.

        Raises:
            InvalidTransition(reason="unknown_state"): `dest` is not declared.
            InvalidTransition(reason="no_edge"):       no edge from current
                                                       state to `dest` exists.
            GuardRejected(reason="guard_rejected"):    guards blocked every
                                                       candidate edge to `dest`.
            GuardRejected(reason="no_deterministic_edge"): edges to `dest` exist
                                                       but a higher-priority edge
                                                       shadows them — no guard
                                                       need be involved. (L4)
        """
        if dest not in self._states:
            raise InvalidTransition(f"->{dest}", self._state, reason="unknown_state")

        state = self._state
        # Collect candidate triggers in registration order, dedup, preserving
        # explicit-before-wildcard preference.
        explicit_triggers: List[str] = []
        wildcard_triggers: List[str] = []
        for src, trigger in self._reverse_dispatch.get(dest, ()):
            if src == state and trigger not in explicit_triggers:
                explicit_triggers.append(trigger)
            elif src == WILDCARD and trigger not in wildcard_triggers:
                wildcard_triggers.append(trigger)
        # A trigger with ANY explicit edge from the current state is explicit,
        # whatever the registration order of its wildcard edge. (PER-MOT-005)
        candidates = explicit_triggers + [t for t in wildcard_triggers if t not in explicit_triggers]
        if not candidates:
            raise InvalidTransition(f"->{dest}", state, reason="no_edge")

        # For each candidate trigger, predict what trigger() would actually fire
        # by walking _match_candidates in priority order with guards evaluated
        # against a frozen view. Only commit if the winning edge's dest matches.
        frozen = MappingProxyType({**self.ctx, **kwargs})
        shadowed = False
        for trigger in candidates:
            cands = self._match_candidates(trigger)
            first_to_dest = next((i for i, c in enumerate(cands) if c.dest == dest), len(cands))
            for i, tr in enumerate(cands):
                if tr.guard is None or tr.guard(frozen):
                    if tr.dest == dest:
                        # Execute the edge we just selected directly, without a
                        # second guard evaluation via trigger(). (M10)
                        return self._execute(tr, trigger, kwargs)
                    # A guard-passing edge exists but leads elsewhere: shadowed
                    # only if it PRECEDES the first edge to dest; a later one
                    # means the guards on the earlier dest edges are what
                    # blocked (dest edges after it are dead by construction —
                    # lint() reports them). (PER-MOT-004)
                    if i < first_to_dest:
                        shadowed = True
                    break

        # Honest reason: "no_deterministic_edge" when a candidate's winning edge
        # went elsewhere (shadowing — possibly with no guards at all);
        # "guard_rejected" only when guards blocked every candidate. (L4)
        raise GuardRejected(
            f"->{dest}", state,
            reason="no_deterministic_edge" if shadowed else "guard_rejected",
        )

    def undo(self) -> str:
        """Revert to previous state and context. No callbacks fired.

        Restores both state and ctx from before the transition (a deep copy,
        unless shallow_ctx=True, in which case nested mutable values are not
        restored — see Machine.__init__). History is a linear stack — branching
        (redo after undo) is not supported. External side effects (I/O, database
        writes) are NOT reverted. (L2)"""
        if self._depth > 0:
            self._warn_reentrant("undo()", stacklevel=3)
        if not self._history:
            raise MachineError("Nothing to undo")
        state, ctx_snapshot = self._history.pop()
        self._pushes -= 1
        self._state = state
        self.ctx.clear()
        self.ctx.update(ctx_snapshot)
        return self._state

    def reset(self, state: Optional[str] = None, clear_ctx: bool = True) -> None:
        """Reset to a state (default: initial state).

        Args:
            state:     Target state (default: initial).
            clear_ctx: If True (default), clears ctx. Pass False to preserve ctx.
        """
        if self._depth > 0:
            self._warn_reentrant("reset()", stacklevel=3)
        target = self._initial if state is None else state
        if target not in self._states:
            raise MachineError(f"Unknown state '{target}'")
        self._state = target
        self._history.clear()
        if clear_ctx:
            self.ctx.clear()

    # ── Observers ─────────────────────────────────────────────────────────

    def subscribe(self, callback: Callable[..., Any]) -> Callable[..., Any]:
        """Add observer. callback(trigger, src, dst, ctx). Returns callback for unsubscribe.

        Observers run *after* the transition commits (the state has already
        changed), so an exception from an observer propagates out of trigger()
        but does NOT roll the transition back — unlike on_exit/action/on_enter.
        Keep observers side-effect-tolerant, or guard them internally. An
        observer that raises also ends the notification loop: observers
        registered after it do not see that transition. (M9)"""
        if not callable(callback):
            raise MachineError("observer must be callable")  # (PER-MOT-011)
        self._observers.append(callback)
        return callback

    def unsubscribe(self, callback: Callable[..., Any]) -> None:
        """Remove an observer. Idempotent: a no-op if `callback` is not
        currently subscribed (no ValueError), so double/defensive unsubscribe is
        safe. (L6)"""
        if callback in self._observers:
            self._observers.remove(callback)

    # ── Serialization ─────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Serialize runtime state. Includes "initial" since 1.5.0 so that
        round-trips through from_dict() preserve reset() semantics. Older
        snapshots without "initial" remain loadable (initial falls back to
        the snapshotted state, matching pre-1.5.0 behavior).

        Only runtime state is serialized — construction knobs (history_size,
        shallow_ctx) are not. After from_dict()/load_dict(), re-supply them to
        the constructor if they differ from the defaults, or `==` will report
        the rebuilt machine unequal. (M5)

        The returned dict is a real snapshot: ctx and every history ctx are
        deep copies (shallow with shallow_ctx=True, like every other copy in
        that mode). Mutating it never reaches the live ctx or the undo
        history, and a snapshot taken before a transition does not change
        when an action later mutates a nested value in place. A ctx value
        that cannot be deep-copied raises MachineError (use shallow_ctx=True
        for locks, sockets, handles). to_json() does not pay this copy: it
        serializes a read-only view directly (1.6.0)."""
        return {
            "state": self._state,
            "initial": self._initial,
            "ctx": self._snapshot_copy(self.ctx, "ctx"),
            "history": [{"state": s, "ctx": self._snapshot_copy(c, "history entry 'ctx'")}
                        for s, c in self._history],
        }

    @staticmethod
    def _deepcopy_ctx(ctx: Dict[str, Any], what: str) -> Dict[str, Any]:
        """Deep-copy a ctx dict from (untrusted) snapshot input, wrapping copy
        failures in MachineError so load_dict's contract holds — always
        MachineError, never a raw exception, and atomic — even for values whose
        __deepcopy__ raises."""
        try:
            return copy.deepcopy(ctx)
        except Exception as e:
            raise MachineError(f"{what} is not deep-copyable: {e}") from e

    def load_dict(self, data: Dict[str, Any]) -> None:
        """Restore runtime state from a dict.

        Atomic and defensive: the input is fully validated (and copied)
        before any attribute is mutated. A malformed snapshot raises
        MachineError — never a raw KeyError/TypeError — and leaves the machine
        untouched (A2). Nested ctx values are deep-copied so the input dict
        cannot alias internal state, honoring the documented transactional
        safety (A3); with shallow_ctx=True the copy is shallow, like every
        other copy in that mode (1.6.0). History entries are validated first
        and copied only if they are kept: entries dropped by the history_size
        truncation are never copied. (PER-SER-003)

        Note: a ctx nested far beyond sys.getrecursionlimit() makes the deep
        copy hit RecursionError, which _deepcopy_ctx converts into
        MachineError; only from_json propagates json.loads' own RecursionError
        on deeply nested JSON. Validate the size/depth of untrusted snapshots
        before loading. (M4)"""
        if not isinstance(data, dict):
            raise MachineError(f"load_dict expects a dict, got {type(data).__name__}")
        if "state" not in data:
            raise MachineError("Missing 'state' in data")
        s = data["state"]
        if not isinstance(s, str) or s not in self._states:
            raise MachineError(f"Unknown state '{s}'")

        # Validate _initial (if present) into a local — don't mutate self yet.
        # Absent in legacy snapshots → keep the current initial.
        new_initial = self._initial
        if "initial" in data:
            init = data["initial"]
            if not isinstance(init, str) or init not in self._states:
                raise MachineError(f"Unknown initial state '{init}'")
            new_initial = init

        raw_ctx = data.get("ctx", {})
        if not isinstance(raw_ctx, dict):
            raise MachineError(f"'ctx' must be a dict, got {type(raw_ctx).__name__}")
        # Copy up-front (A3), before any mutation (A2): deep by default, dict() with
        # shallow_ctx=True. (PER-SUB-001)
        new_ctx = dict(raw_ctx) if self._shallow_ctx else self._deepcopy_ctx(raw_ctx, "ctx")

        raw_hist = data.get("history", [])
        if not isinstance(raw_hist, list):
            raise MachineError(f"'history' must be a list, got {type(raw_hist).__name__}")
        # First pass only VALIDATES and keeps raw references: nothing is copied
        # until we know which entries survive the truncation below. (PER-SER-003)
        pending: List[Tuple[str, Dict[str, Any]]] = []
        for entry in raw_hist:
            if isinstance(entry, dict):
                # New format: {"state": "...", "ctx": {...}}
                if "state" not in entry:
                    raise MachineError(f"history entry missing 'state': {entry!r}")
                h = entry["state"]
                if not isinstance(h, str) or h not in self._states:
                    raise MachineError(f"Unknown state '{h}' in history")
                h_ctx = entry.get("ctx", {})
                if not isinstance(h_ctx, dict):
                    raise MachineError(
                        f"history entry 'ctx' must be a dict, got {type(h_ctx).__name__}")
                pending.append((h, h_ctx))
            elif isinstance(entry, str):
                # Legacy format: plain state string (no ctx snapshot)
                if entry not in self._states:
                    raise MachineError(f"Unknown state '{entry}' in history")
                pending.append((entry, {}))
            else:
                raise MachineError(f"Invalid history entry: {entry!r}")

        # Truncate to history_size to prevent unbounded memory from untrusted input.
        # 1.5.0: silent truncation is deprecated. 2.0.0 will raise instead.
        maxlen: Optional[int]
        if self._history_size > 0:
            if len(pending) > self._history_size:
                warnings.warn(
                    f"history truncated from {len(pending)} to {self._history_size} entries; "
                    f"will raise MachineError in tramoya 2.0",
                    DeprecationWarning,
                    stacklevel=2,
                )
                pending = pending[-self._history_size:]
            maxlen = self._history_size
        else:
            # history_size == 0 → undo disabled (_push_history is a no-op). Drop
            # any snapshot history instead of loading it unbounded from
            # (untrusted) input, which the maxlen=None deque would otherwise
            # accept in full. (M3)
            pending = []
            maxlen = None

        # Copy only the entries that are kept, same deep/shallow rule as ctx above.
        # (PER-SER-003)
        history = [
            (h, dict(h_ctx) if self._shallow_ctx
             else self._deepcopy_ctx(h_ctx, "history entry 'ctx'"))
            for h, h_ctx in pending
        ]

        # All input validated and copied — commit atomically; nothing below can fail.
        self._initial = new_initial
        self._state = s
        self.ctx.clear()
        self.ctx.update(new_ctx)
        self._history = deque(history, maxlen=maxlen)

    @classmethod
    def from_dict(
        cls,
        states: List[str],
        transitions: List[Union[Tuple[Any, ...], _Transition]],
        data: Dict[str, Any],
        **kwargs: Any,
    ) -> "Machine":
        """Reconstruct machine from serialized dict.

        kwargs are forwarded to __init__ (on_enter, on_exit, on_transition,
        history_size, shallow_ctx). Passing `ctx` is a no-op — ctx is restored
        from `data` — and emits DeprecationWarning since 1.5.0; will raise in
        2.0.

        Initial state resolution: uses data["initial"] if present (1.5.0+
        snapshots), falls back to data["state"] for legacy snapshots. The
        legacy fallback preserves the pre-1.5.0 behavior where reset() after
        from_dict() landed on the snapshotted state, not the original initial.
        """
        if "ctx" in kwargs:
            warnings.warn(
                "ctx kwarg passed to from_dict() is ignored — ctx is restored from data. "
                "Will raise TypeError in tramoya 2.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            kwargs.pop("ctx")
        # Structural validation before touching data: wrong shapes raise
        # MachineError, never a raw KeyError/AttributeError/TypeError. (PER-SER-001)
        if not isinstance(data, dict):
            raise MachineError(f"from_dict expects a dict, got {type(data).__name__}")
        if "state" not in data:
            raise MachineError("Missing 'state' in data")
        initial = data.get("initial", data["state"])
        if not isinstance(initial, str):
            raise MachineError(f"Unknown initial state '{initial}'")
        m = cls(states=states, transitions=transitions, initial=initial, **kwargs)
        m.load_dict(data)
        return m

    def to_json(self) -> str:
        """Serialize to_dict() as a JSON string.

        ctx must be JSON-native: non-string keys become strings, tuples become
        lists and NaN is emitted as the non-standard literal; non-serializable
        values raise TypeError from json.dumps. (PER-DOC-002)

        json.dumps never mutates its input and yields its own immutable text,
        so the deep copy to_dict() makes would be redundant here: serialize a
        shallow view instead (same output, no copy cost). Note that this does
        not call to_dict(): a subclass overriding to_dict() must also override
        to_json() if it wants the two to agree."""
        view = {
            "state": self._state,
            "initial": self._initial,
            "ctx": self.ctx,
            "history": [{"state": s, "ctx": c} for s, c in self._history],
        }
        return json.dumps(view, ensure_ascii=False)

    @classmethod
    def from_json(
        cls,
        states: List[str],
        transitions: List[Union[Tuple[Any, ...], _Transition]],
        json_str: str,
        **kwargs: Any,
    ) -> "Machine":
        """Reconstruct machine from a JSON string (see from_dict).

        Malformed JSON raises `json.JSONDecodeError` (a `ValueError`) from
        `json.loads`, unchanged; a well-formed document with the wrong structure
        raises `MachineError` (1.6.0)."""
        return cls.from_dict(states, transitions, json.loads(json_str), **kwargs)

    # ── Graph export ──────────────────────────────────────────────────────

    @staticmethod
    def _dot_escape(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _mermaid_id(s: str) -> str:
        """Sanitize state name for Mermaid node ID (alphanumeric + underscore)."""
        return "".join(c if c.isalnum() or c == "_" else "_" for c in s)

    @staticmethod
    def _mermaid_label(s: str) -> str:
        """Escape characters that break Mermaid stateDiagram-v2 transition labels.

        Mermaid uses ":" to separate the edge from its label, "|" inside choice
        nodes, and parses HTML in labels. Without escaping, a trigger named
        "price>50" or "foo:bar" silently breaks the render (blank diagram, no
        parse error). HTML entities render correctly in Mermaid output.

        Every line separator (CR, CRLF, VT, FF, NEL, LS, PS) is first
        normalized to a newline: a bare CR or Unicode separator would otherwise
        split the edge line and let a trigger name smuggle a second statement
        into the diagram. (PER-EXP-001)

        Replace order matters: "&" first, otherwise the entities we insert
        below would themselves be escaped."""
        s = re.sub(r"\r\n?|[\x0b\x0c\x85\u2028\u2029]", "\n", s)
        return (s
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace(":", "&#58;")
                .replace("|", "&#124;")
                .replace('"', "&quot;")
                .replace("\n", "<br/>"))

    def to_dot(self, title: str = "machine") -> str:
        """Export to Graphviz DOT format."""
        esc = self._dot_escape
        # Quote the title: an unquoted DOT id breaks on common titles like
        # "order-machine" or "my machine", and "{"/"}" could inject graph
        # structure. _dot_escape already handles the embedded '"' and '\'. (M11)
        lines = [
            f'digraph "{esc(title)}" {{',
            '  rankdir=LR;',
            '  node [shape=circle];',
            f'  "{esc(self._state)}" [shape=doublecircle, style=filled, fillcolor=lightblue];',
        ]
        for trigger, trans in self._transitions.items():
            for tr in trans:
                label = esc(trigger)
                if tr.guard:
                    label += " [guarded]"
                if tr.source == WILDCARD:
                    for s in sorted(self._states):
                        dst = esc(tr.dest) if tr.dest else esc(s)
                        style = 'style=dashed' if tr.dest else 'style=dotted'
                        lines.append(f'  "{esc(s)}" -> "{dst}" [label="{label}", {style}];')
                else:
                    src = esc(tr.source)
                    dst = esc(tr.dest) if tr.dest else src
                    style = '' if tr.dest else ', style=dotted'
                    lines.append(f'  "{src}" -> "{dst}" [label="{label}"{style}];')
        lines.append("}")
        return "\n".join(lines)

    def _mermaid_ids(self) -> Dict[str, str]:
        """Map each state to a unique, Mermaid-safe node id. _mermaid_id is not
        injective ('a-b' and 'a_b' both sanitize to 'a_b'), which would merge
        distinct states into one node and misroute edges; on collision append a
        numeric suffix so ids stay 1:1 with states. Deterministic (sorted). (M12)"""
        ids: Dict[str, str] = {}
        used: Set[str] = set()
        for s in sorted(self._states):
            base = self._mermaid_id(s)
            candidate = base
            i = 2
            while candidate in used:
                candidate = f"{base}__{i}"
                i += 1
            ids[s] = candidate
            used.add(candidate)
        return ids

    def to_mermaid(self) -> str:
        """Export to Mermaid stateDiagram-v2 format.

        Node ids are made unique via _mermaid_ids() so distinct states that
        sanitize to the same id stay separate; a state whose id differs from its
        name gets an explicit node declaration carrying the real name. Trigger
        names are escaped via _mermaid_label() so characters like ":", "|", "<",
        ">" don't silently break the render."""
        ids = self._mermaid_ids()
        mlabel = self._mermaid_label
        lines = ["stateDiagram-v2"]
        # Declare nodes whose sanitized id differs from the real name, so the
        # diagram shows the true name and colliding states stay distinct.
        for s in sorted(self._states):
            if ids[s] != s:
                lines.append(f'    state "{mlabel(s)}" as {ids[s]}')
        for trigger, trans in self._transitions.items():
            for tr in trans:
                label = mlabel(trigger)
                if tr.guard:
                    label += " [guarded]"
                if tr.source == WILDCARD:
                    # Expand wildcard to edges from every state
                    for s in sorted(self._states):
                        dst = ids[tr.dest] if tr.dest else ids[s]
                        lines.append(f"    {ids[s]} --> {dst} : {label}")
                else:
                    src = ids[tr.source]
                    dst = ids[tr.dest] if tr.dest else src
                    lines.append(f"    {src} --> {dst} : {label}")
        return "\n".join(lines)

    # ── Lint ──────────────────────────────────────────────────────────────

    def _lint_candidates(self, state: str, trigger: str) -> List[_Transition]:
        """L(state, trigger): same "explicit first, then wildcard, each in
        registration order" list _match_candidates builds for self._state —
        but for any declared state, since lint() reasons about all of them,
        not just the live one."""
        explicit = self._dispatch.get((state, trigger), ())
        wildcard = self._wildcard_dispatch.get(trigger, ())
        if not explicit:
            return list(wildcard)
        if not wildcard:
            return list(explicit)
        return [*explicit, *wildcard]

    @staticmethod
    def _lint_sweep(
        edges: List[_Transition], shadow_of: Dict[int, _Transition]
    ) -> None:
        """One linear pass over a candidate list: map every edge registered
        after the list's FIRST unguarded edge to that edge (its shadower).
        Edges before it are all guarded (alive), and the first unguarded edge
        itself is never shadowed — nothing before it is unguarded — which is
        exactly the "first, not nearest" rule lint() documents.

        Keys are id(edge), not the edge: duplicate edges compare equal
        (frozen dataclass), and only the *second* occurrence is dead."""
        first: Optional[_Transition] = None
        for e in edges:
            if first is not None:
                shadow_of[id(e)] = first
            elif e.guard is None:
                first = e

    def _lint_wildcard_dead_everywhere(self, trigger: str) -> bool:
        """True iff every declared state has its own unguarded explicit edge
        for `trigger` — the wildcard can then never win a dispatch from any
        state, so it's dead even though no single edge shadows it."""
        return all(
            any(e.guard is None for e in self._dispatch.get((s, trigger), ()))
            for s in self._states
        )

    def _lint_dead_edges(self) -> List[LintFinding]:
        """dead_edge findings, walked in self._transitions order (trigger
        insertion order, then each trigger's edges in registration order) —
        see lint() for the shadowing rule.

        Linear in the number of edges: each candidate list is swept exactly
        once (_lint_sweep) instead of re-scanning it per edge, and the
        every-state wildcard check runs at most once per trigger (memoized)
        instead of once per wildcard edge. Findings still emit in registration
        order because the emit loop walks `trans`, not the sweep."""
        findings: List[LintFinding] = []
        for trigger, trans in self._transitions.items():
            shadow_of: Dict[int, _Transition] = {}
            self._lint_sweep(self._wildcard_dispatch.get(trigger, []), shadow_of)
            for src in {tr.source for tr in trans if tr.source != WILDCARD}:
                self._lint_sweep(self._dispatch.get((src, trigger), []), shadow_of)

            wc_dead_everywhere: Optional[bool] = None  # lazy: at most one O(V) check per trigger
            for tr in trans:
                shadow = shadow_of.get(id(tr))
                if tr.source == WILDCARD:
                    if shadow is not None:
                        findings.append(LintFinding(
                            kind="dead_edge", severity="warning",
                            message=(
                                f"trigger {trigger!r} from '*' to {tr.dest!r} is "
                                f"always shadowed by an earlier unguarded wildcard "
                                f"edge (-> {shadow.dest!r})"),
                            trigger=trigger, source=WILDCARD, dest=tr.dest,
                            shadowed_by=(shadow.trigger, shadow.source, shadow.dest),
                        ))
                        continue
                    if wc_dead_everywhere is None:
                        wc_dead_everywhere = self._lint_wildcard_dead_everywhere(trigger)
                    if wc_dead_everywhere:
                        findings.append(LintFinding(
                            kind="dead_edge", severity="warning",
                            message=(
                                f"trigger {trigger!r} from '*' to {tr.dest!r} is "
                                f"shadowed in every state by that state's own "
                                f"unguarded explicit edge for {trigger!r}"),
                            trigger=trigger, source=WILDCARD, dest=tr.dest,
                            shadowed_by=None,
                        ))
                elif shadow is not None:
                    findings.append(LintFinding(
                        kind="dead_edge", severity="warning",
                        message=(
                            f"trigger {trigger!r} from {tr.source!r} to "
                            f"{tr.dest!r} can never fire: shadowed by an "
                            f"earlier unguarded edge (-> {shadow.dest!r})"),
                        trigger=trigger, source=tr.source, dest=tr.dest,
                        shadowed_by=(shadow.trigger, shadow.source, shadow.dest),
                    ))
        return findings

    def _lint_reachability(self) -> Tuple[Set[str], Dict[str, bool]]:
        """BFS from initial using only fireable-from-s edges (see lint()).
        Returns (reachable, has_exit): has_exit[s] is True if some fireable
        edge from s lands in a declared state other than s (self-loops and
        internal transitions don't count). Computed together so the
        traversal — and the per-state candidate lookup it needs — runs once
        and serves both unreachable_state and sink_state."""
        reachable: Set[str] = {self._initial}
        has_exit: Dict[str, bool] = {}
        queue: Deque[str] = deque([self._initial])
        while queue:
            s = queue.popleft()
            exits = False
            for trigger in self._transitions:
                blocked = False
                for tr in self._lint_candidates(s, trigger):
                    if not blocked:
                        if tr.dest is not None and tr.dest != s:
                            exits = True
                        if tr.dest is not None and tr.dest not in reachable:
                            reachable.add(tr.dest)
                            queue.append(tr.dest)
                    if tr.guard is None:
                        blocked = True
            has_exit[s] = exits
        return reachable, has_exit

    def lint(self, include_info: bool = False) -> List[LintFinding]:
        """Static analysis of the machine's trigger topology. Pure: never
        raises, never mutates state/ctx/history/indices, and two consecutive
        calls return equal lists.

        Mirrors the runtime dispatch rule from _match_candidates: for a given
        (state, trigger), the first candidate edge with guard=None always
        wins, so anything registered after it can never fire. A guard is
        treated as opaque — it might return False at runtime — so a guarded
        edge never shadows anything. That makes the analysis optimistic: zero
        false positives by design, but it can under-report (a guard that
        always returns False is not detectable statically).

        Findings (see LintFinding):
          - dead_edge (warning): an edge that can never fire because a
            higher-priority unguarded edge for the same trigger always wins
            first. For an explicit edge, shadowed_by is the first earlier
            unguarded edge from the same (source, trigger) — which is, by
            construction, never itself dead. A wildcard edge is dead when no
            declared state can ever reach it: either an earlier unguarded
            wildcard for the same trigger shadows it everywhere
            (shadowed_by set), or every declared state has its own unguarded
            explicit edge for that trigger (shadowed_by=None — no single
            edge is responsible; if both causes apply, the first wins).
            Internal transitions (dest=None) participate fully: an unguarded
            internal edge consumes the trigger just like any other, and can
            itself be dead the same way.
          - unreachable_state (warning): no path from `initial` reaches this
            state using only fireable edges. A wildcard edge that is alive
            globally can still fail to contribute a successor from a
            specific state where it is locally shadowed by that state's own
            unguarded explicit edge — the BFS accounts for this per state.
            `initial` is always reachable.
          - sink_state (info, only with include_info=True): a reachable
            state with no fireable edge leading anywhere but itself
            (self-loops and internal transitions don't count as an exit).
            Reported as info, not warning: tramoya has no notion of a
            "final" state, so a dead end may be entirely intentional.
            Unreachable states are never also reported as sinks (same root
            cause, reported once as unreachable_state).

        Order: all dead_edge findings first (self._transitions insertion
        order, then each trigger's edges in registration order), then
        unreachable_state sorted by state name, then — only with
        include_info=True — sink_state sorted by state name.

        Args:
            include_info: If True, also include "info"-severity findings
                          (currently just sink_state). Default False.
        """
        findings = self._lint_dead_edges()

        reachable, has_exit = self._lint_reachability()
        findings.extend(
            LintFinding(
                kind="unreachable_state", severity="warning",
                message=f"state {s!r} is not reachable from initial state {self._initial!r}",
                state=s,
            )
            for s in sorted(self._states - reachable)
        )

        if include_info:
            findings.extend(
                LintFinding(
                    kind="sink_state", severity="info",
                    message=f"state {s!r} is reachable but has no fireable edge leaving it",
                    state=s,
                )
                for s in sorted(reachable)
                if not has_exit[s]
            )

        return findings

    def _transition_topology(self) -> Dict[str, List[Tuple[str, Optional[str], bool, bool]]]:
        """Extract transition topology for equality comparison.
        Returns {trigger: [(source, dest, has_guard, has_action), ...]}."""
        return {
            trigger: [(tr.source, tr.dest, tr.guard is not None, tr.action is not None) for tr in trans]
            for trigger, trans in self._transitions.items()
        }

    def __eq__(self, other: object) -> bool:
        """Structural + runtime equality: same state, initial, ctx, history,
        declared states, transition topology, and construction knobs
        (history_size, shallow_ctx). Including the knobs means a serialization round-trip that
        silently dropped them is detectable via `==` (M5).

        Topology compares whether each edge *has* a guard/action, not the
        callables' behavior: two machines whose guards return differently but
        match everywhere else compare equal. Comparing callable identity would
        instead make structurally-identical machines built from separate lambdas
        unequal, which is rarely what callers want. (M7)"""
        if not isinstance(other, Machine):
            return NotImplemented
        return (self._state == other._state
                and self._initial == other._initial  # (PER-EQU-001)
                and self.ctx == other.ctx
                and self._history == other._history
                and self._states == other._states
                and self._history_size == other._history_size
                and self._shallow_ctx == other._shallow_ctx
                and self._transition_topology() == other._transition_topology())

    # Machine is a mutable entity, so equality-by-value cannot back a stable
    # by-value hash. Restore *identity* hashing — defining __eq__ otherwise sets
    # __hash__ to None, making instances unhashable (no set/dict/lru_cache use
    # at all). Trade-off: hashing by identity while __eq__ compares by value
    # means two value-equal machines are DISTINCT keys — a set won't dedupe
    # them and dict lookup by a value-equal (but non-identical) machine won't
    # hit. Use machines in sets/dicts as identity handles, not value keys. (M6)
    __hash__ = object.__hash__

    def __repr__(self) -> str:
        return f"Machine(state={self._state!r}, triggers={self.available_triggers})"


# ─── SubMachine (Hierarchical States) ────────────────────────────────────────

class SubMachine:
    """
    Wrap a Machine as a hierarchical (nested) state inside a parent Machine.

    Usage:
        inner = Machine(states=["a","b"], transitions=[("go","a","b")], initial="a")
        sub = SubMachine("processing", inner)

        parent = Machine(
            states=["idle", "processing", "done"],
            transitions=[
                ("start", "idle", "processing"),
                ("finish", "processing", "done"),
            ],
            initial="idle",
            on_enter={"processing": sub.enter},
            on_exit={"processing": sub.exit},
        )

        # When parent enters "processing", inner resets to its initial state.
        # Access inner state: sub.machine.state
        # Trigger inner: sub.machine.trigger("go") or sub.trigger("go")

    shared_keys are copied parent→child on enter() only (deep copy in the
    default mode, by reference with shallow_ctx=True on the inner machine);
    nothing is copied back to the parent on exit(), and restore() reloads the
    inner ctx exactly as it was saved at exit(), shared keys included.
    (PER-CMP-002)

    SubMachine state is not part of the parent's to_dict()/load_dict()
    snapshot: persist the inner machine separately (planned for 1.8).
    (PER-CMP-003)
    """

    def __init__(self, parent_state: str, machine: Machine, shared_keys: Optional[List[str]] = None):
        self.parent_state = parent_state
        self.machine = machine
        self._saved: Optional[Dict[str, Any]] = None
        self._shared_keys: Optional[List[str]] = shared_keys

    def enter(self, ctx: Dict[str, Any]) -> None:
        """Called when parent enters this state. Fully resets inner machine
        (state, history, and ctx). Only copies shared_keys from parent ctx
        if specified."""
        self.machine.reset()  # reset(clear_ctx=True) already clears ctx (L8)
        if self._shared_keys:
            for key in self._shared_keys:
                if key in ctx:
                    value = ctx[key]
                    if self.machine._shallow_ctx:
                        # By reference, like every copy in shallow_ctx mode. (PER-SUB-001)
                        self.machine.ctx[key] = value
                    else:
                        try:
                            self.machine.ctx[key] = copy.deepcopy(value)
                        except Exception as e:
                            raise MachineError(
                                f"shared key {key!r} is not deep-copyable: {e}") from e

    def exit(self, ctx: Dict[str, Any]) -> None:
        """Called when parent exits this state. Saves inner snapshot."""
        self._saved = self.machine.to_dict()

    def restore(self) -> None:
        """Restore last saved inner state (after re-entering parent state)."""
        if self._saved:
            self.machine.load_dict(self._saved)

    def trigger(self, name: str, **kwargs: Any) -> str:
        """Propagate a trigger to the inner machine."""
        return self.machine.trigger(name, **kwargs)

    def __repr__(self) -> str:
        return f"SubMachine({self.parent_state!r}, inner={self.machine.state!r})"


# ─── ParallelMachine (Orthogonal Regions) ───────────────────────────────────

class ParallelMachine:
    """
    Run multiple Machines in parallel (orthogonal regions).

    Each region evolves independently. A trigger is broadcast to all regions
    that can handle it. The composite state is the tuple of all region states.

    Usage:
        movement = Machine(states=["idle","walking","running"], ...)
        combat   = Machine(states=["peaceful","attacking","defending"], ...)

        player = ParallelMachine(movement=movement, combat=combat)

        player.trigger("walk")      # only movement handles it
        player.trigger("attack")    # only combat handles it
        print(player.state)         # {"movement": "walking", "combat": "attacking"}

        player.trigger("reset")     # both handle it if they have that trigger
    """

    def __init__(self, **regions: Machine):
        if not regions:
            raise MachineError("ParallelMachine requires at least one region")
        self._regions: Dict[str, Machine] = regions

    @property
    def regions(self) -> Mapping[str, Machine]:
        """Read-only view of regions."""
        return MappingProxyType(self._regions)

    @property
    def state(self) -> Dict[str, str]:
        """Composite state: {region_name: current_state}."""
        return {name: m.state for name, m in self._regions.items()}

    @property
    def ctx(self) -> Dict[str, Dict[str, Any]]:
        """Context per region: {region_name: ctx}."""
        return {name: m.ctx for name, m in self._regions.items()}

    def can(self, trigger: str, **kwargs: Any) -> bool:
        """True if at least one region can handle this trigger."""
        return any(m.can(trigger, **kwargs) for m in self._regions.values())

    def trigger(self, name: str, **kwargs: Any) -> Dict[str, str]:
        """Broadcast trigger to all regions that can handle it.
        Returns composite state. Raises InvalidTransition if NO region has the
        trigger, GuardRejected if some region has it but its guards blocked it
        (PER-CMP-005).

        Not atomic across regions: regions fire in insertion order and each
        commits on its own. If a region's callback raises, the regions fired
        before it stay committed and the exception propagates — same contract
        as trigger_many (L9). (PER-CMP-004)

        Note: guards are evaluated twice per region (once in can(), once in
        trigger()). Guards must be pure functions — side effects in guards
        (I/O, counters, logging) will execute twice. Regions fire in insertion
        order; keep them truly independent (no shared external state read by
        guards) or the outcome becomes order-dependent. (L5)"""
        fired = False
        for m in self._regions.values():
            if m.can(name, **kwargs):
                m.trigger(name, **kwargs)
                fired = True
        if not fired:
            states = ", ".join(f"{k}={v.state}" for k, v in self._regions.items())
            if any(name in m.available_triggers for m in self._regions.values()):
                raise GuardRejected(name, f"[{states}]")  # (PER-CMP-005)
            raise InvalidTransition(name, f"[{states}]")
        return self.state

    def trigger_region(self, region: str, name: str, **kwargs: Any) -> str:
        """Fire a trigger in a specific region only."""
        if region not in self._regions:
            raise MachineError(f"Unknown region '{region}'")
        return self._regions[region].trigger(name, **kwargs)

    def undo(self, region: Optional[str] = None) -> Dict[str, str]:
        """Undo last transition. If region specified, undo only that region.
        Otherwise (region=None) undo ALL regions (each one step back)."""
        if region is not None:  # (PER-CMP-005): "" is an unknown region, not "all"
            if region not in self._regions:
                raise MachineError(f"Unknown region '{region}'")
            self._regions[region].undo()
        else:
            for m in self._regions.values():
                if m.history:
                    m.undo()
        return self.state

    def reset(self) -> None:
        """Reset all regions to their initial states."""
        for m in self._regions.values():
            m.reset()

    def to_dict(self) -> Dict[str, Any]:
        return {name: m.to_dict() for name, m in self._regions.items()}

    def load_dict(self, data: Dict[str, Any]) -> None:
        """Restore all regions atomically. Validates region names first, then
        takes an identity-preserving snapshot of each region named in `data`
        (no deepcopy), so a failure partway through (an invalid region payload)
        puts the regions already loaded back to their pre-call state — the SAME
        ctx objects, not clones — instead of leaving the composite machine torn
        (A2). Regions not named in `data` are never touched or copied, so an
        un-deep-copyable ctx value elsewhere cannot break the rollback.
        (PER-CMP-006)"""
        if not isinstance(data, dict):
            raise MachineError(f"load_dict expects a dict, got {type(data).__name__}")
        for name in data:
            if name not in self._regions:
                raise MachineError(f"Unknown region '{name}' in data")
        snapshots = {name: self._regions[name]._runtime_snapshot() for name in data}
        loaded: List[str] = []
        try:
            for name, region_data in data.items():
                self._regions[name].load_dict(region_data)
                loaded.append(name)
        except BaseException:
            # Put the SAME objects back (no deepcopy): cannot fail on
            # un-deep-copyable ctx values and keeps external references into
            # ctx valid; only regions already loaded need restoring, each
            # Machine.load_dict is atomic on its own. (PER-CMP-006)
            for name in loaded:
                self._regions[name]._runtime_restore(snapshots[name])
            raise

    def __repr__(self) -> str:
        parts = ", ".join(f"{k}={v.state!r}" for k, v in self._regions.items())
        return f"ParallelMachine({parts})"


# ─── MachineBuilder (Decorator API) ──────────────────────────────────────────

class MachineBuilder:
    """
    Build a Machine using decorators.

    Usage:
        b = MachineBuilder("idle")
        b.add_states("idle", "running", "done")

        @b.on("start", "idle", "running")
        def on_start(ctx):
            print("Started!")

        @b.guard("start", "idle", "running")
        def is_ready(ctx):
            return ctx.get("ready", False)

        @b.enter("running")
        def entering_running(ctx):
            print("Now running")

        machine = b.build()
    """

    def __init__(self, initial: str):
        self._initial = initial
        self._states: List[str] = []
        self._transitions: List[Tuple[str, str, Optional[str]]] = []
        self._on_enter: Dict[str, Callable[..., Any]] = {}
        self._on_exit: Dict[str, Callable[..., Any]] = {}
        self._guards: Dict[Tuple[str, str, Optional[str]], Callable[..., Any]] = {}
        self._actions: Dict[Tuple[str, str, Optional[str]], Callable[..., Any]] = {}
        self._on_transition: Optional[Callable[..., Any]] = None
        self._observers: List[Callable[..., Any]] = []

    def add_states(self, *names: str) -> "MachineBuilder":
        for name in names:
            if name not in self._states:
                self._states.append(name)
        return self

    def transition(self, trigger: str, source: str, dest: Optional[str]) -> "MachineBuilder":
        """Add a plain transition (no guard/action)."""
        self._transitions.append((trigger, source, dest))
        return self

    def on(self, trigger: str, source: str, dest: Optional[str]) -> Callable[..., Any]:
        """Decorator: register action for a transition."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            key = (trigger, source, dest)
            if key in self._actions:
                warnings.warn(
                    f"action for {key!r} already registered; the last one wins — "
                    f"will raise MachineError in tramoya 2.0",
                    DeprecationWarning,
                    stacklevel=2,
                )  # (PER-BLD-002)
            self._actions[key] = fn
            return fn
        return decorator

    def guard(self, trigger: str, source: str, dest: Optional[str]) -> Callable[..., Any]:
        """Decorator: register guard for a transition."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            key = (trigger, source, dest)
            if key in self._guards:
                warnings.warn(
                    f"guard for {key!r} already registered; the last one wins — "
                    f"will raise MachineError in tramoya 2.0",
                    DeprecationWarning,
                    stacklevel=2,
                )  # (PER-BLD-002)
            self._guards[key] = fn
            return fn
        return decorator

    def enter(self, state: str) -> Callable[..., Any]:
        """Decorator: register on_enter callback."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._on_enter[state] = fn
            return fn
        return decorator

    def exit(self, state: str) -> Callable[..., Any]:
        """Decorator: register on_exit callback."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._on_exit[state] = fn
            return fn
        return decorator

    def observe(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator: register observer callback(trigger, src, dst, ctx)."""
        self._observers.append(fn)
        return fn

    def on_transition_handler(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator: register global on_transition callback(trigger, src, dst, ctx)."""
        self._on_transition = fn
        return fn

    def build(self, **kwargs: Any) -> Machine:
        """Build and return the Machine. Transition order is deterministic:
        plain transitions first (in registration order), then decorator-only
        transitions — all guard-only keys (in registration order), then any
        remaining action-only keys. So for two competing decorator-only edges, a
        @guard-registered one is ordered before an @on-registered one regardless
        of which was declared first. (L7)"""
        # Ensure initial state is included
        self.add_states(self._initial)

        transitions: List[Tuple[Any, ...]] = []
        seen_keys: Set[Tuple[str, str, Optional[str]]] = set()

        # Plain transitions first — preserves registration order
        for t in self._transitions:
            key = (t[0], t[1], t[2])
            g = self._guards.get(key)
            a = self._actions.get(key)
            transitions.append((t[0], t[1], t[2], g, a))
            seen_keys.add(key)

        # Then decorator-only transitions (guard/action without plain transition)
        # Use _guards insertion order first, then _actions for keys not yet seen
        for key in self._guards:
            if key not in seen_keys:
                trigger, src, dst = key
                a = self._actions.get(key)
                transitions.append((trigger, src, dst, self._guards[key], a))
                seen_keys.add(key)
        for key in self._actions:
            if key not in seen_keys:
                trigger, src, dst = key
                g = self._guards.get(key)
                transitions.append((trigger, src, dst, g, self._actions[key]))
                seen_keys.add(key)

        m = Machine(
            states=self._states,
            transitions=transitions,  # type: ignore[arg-type]
            initial=self._initial,
            # Own copies: Machine keeps the dict it is given, so a hook
            # registered after build() must not leak into it. (PER-BLD-001)
            on_enter=dict(self._on_enter) or None,
            on_exit=dict(self._on_exit) or None,
            on_transition=self._on_transition,
            **kwargs,
        )
        for obs in self._observers:
            m.subscribe(obs)
        return m


# ─── Quick test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # ── Basic usage ──
    order = Machine(
        states=["draft", "submitted", "approved", "rejected"],
        transitions=[
            ("submit",  "draft",     "submitted"),
            ("approve", "submitted", "approved",  lambda ctx: ctx.get("score", 0) > 50),
            ("reject",  "submitted", "rejected"),
            ("revise",  "rejected",  "draft"),
            ("cancel",  "*",         "rejected"),   # wildcard
            ("log",     "*",         None,          None, lambda ctx: print(f"    [log] ctx={ctx}")),  # internal
        ],
        initial="draft",
        on_transition=lambda t, s, d, c: print(f"    >> {s} --{t}--> {d}"),
    )

    print(f"tramoya v{__version__} demo:")
    print(f"  state = {order.state}")
    order.trigger("submit")
    print(f"  submit → {order.state}")
    print(f"  can approve (score=30)? {order.can('approve', score=30)}")
    print(f"  can approve (score=80)? {order.can('approve', score=80)}")
    order.trigger("approve", score=80)
    print(f"  approve → {order.state}")
    order.undo()
    print(f"  undo → {order.state}")
    print(f"  is_final? {order.is_final}")

    # Wildcard
    order.trigger("cancel")
    print(f"  cancel (wildcard) → {order.state}")

    # Internal transition
    order.reset("draft")
    order.trigger("log")
    print(f"  log (internal) → {order.state}  (still draft)")

    # Batch
    order.reset("draft")
    order.trigger_many("submit", ("approve", {"score": 99}))
    print(f"  batch submit+approve → {order.state}")

    # Serialization round-trip
    snap = order.to_json()
    print(f"  json = {snap}")

    # Mermaid
    print(f"  Mermaid:\n{order.to_mermaid()}")

    # ── Builder API ──
    print("\n--- Builder API ---")
    b = MachineBuilder("off")
    b.add_states("off", "on", "turbo")
    b.transition("toggle", "off", "on")
    b.transition("toggle", "on", "off")
    b.transition("boost", "on", "turbo")

    @b.enter("on")
    def _enter_on(ctx: Dict[str, Any]) -> None:
        print("    [enter] lights on!")

    @b.guard("boost", "on", "turbo")
    def _need_fuel(ctx: Mapping[str, Any]) -> bool:
        return bool(ctx.get("fuel", 0) > 10)

    lamp = b.build()
    lamp.trigger("toggle")
    print(f"  toggle → {lamp.state}")
    print(f"  can boost (fuel=5)? {lamp.can('boost', fuel=5)}")
    print(f"  can boost (fuel=20)? {lamp.can('boost', fuel=20)}")
    lamp.trigger("boost", fuel=20)
    print(f"  boost → {lamp.state}")

    # ── SubMachine ──
    print("\n--- SubMachine (Hierarchical) ---")
    inner = Machine(
        states=["step1", "step2", "step3"],
        transitions=[("next", "step1", "step2"), ("next", "step2", "step3")],
        initial="step1",
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
    print(f"  parent={parent.state}, inner={sub.machine.state}")
    sub.machine.trigger("next")
    print(f"  inner next → {sub.machine.state}")
    sub.machine.trigger("next")
    print(f"  inner next → {sub.machine.state}")
    parent.trigger("finish")
    print(f"  parent={parent.state} (inner saved: {sub._saved})")