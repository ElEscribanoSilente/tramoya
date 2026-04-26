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

Limitations:
  - Not thread-safe: no locking on state/ctx/history mutations
  - No async support: callbacks are synchronous only
  - SubMachine has no automatic done-state propagation to parent

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
import json
import warnings
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Set, Tuple, Union

__version__ = "1.5.0"
__all__ = [
    "Machine", "MachineBuilder", "SubMachine", "ParallelMachine", "WILDCARD",
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

class GuardRejected(MachineError):
    """All candidate transitions for the trigger were blocked by guards.

    Attributes:
        trigger: Trigger name that was attempted.
        state:   State the machine was in.
        reason:  Always "guard_rejected". Provided for parity with
                 InvalidTransition.reason for typed dispatch on errors.
    """
    def __init__(self, trigger: str, state: str, reason: str = "guard_rejected"):
        self.trigger, self.state, self.reason = trigger, state, reason
        super().__init__(f"Guard rejected '{trigger}' from '{state}'")


# ─── Transition ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Transition:
    trigger: str
    source: str                                                    # state name or "*" for wildcard
    dest: Optional[str]                                            # None = internal transition
    guard: Optional[Callable[[Mapping[str, Any]], bool]] = None    # receives read-only view
    action: Optional[Callable[[Dict[str, Any]], Any]] = None       # receives mutable ctx


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
        ctx:           Arbitrary context dict carried through the machine.
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
        self.ctx: Dict[str, Any] = ctx or {}
        self._history: Deque[Tuple[str, Dict[str, Any]]] = deque(maxlen=history_size if history_size > 0 else None)
        self._history_size = history_size
        self._shallow_ctx = shallow_ctx
        self._observers: List[Callable[..., Any]] = []

        if initial not in self._states:
            raise MachineError(f"Initial state '{initial}' not in states")

        for key in self._on_enter:
            if key not in self._states:
                raise MachineError(f"on_enter references unknown state '{key}'")
        for key in self._on_exit:
            if key not in self._states:
                raise MachineError(f"on_exit references unknown state '{key}'")

        for t in transitions:
            self._add_transition(t)

    # ── Internal ──────────────────────────────────────────────────────────

    def _add_transition(self, t: Union[Tuple[Any, ...], _Transition]) -> None:
        if isinstance(t, _Transition):
            tr = t
        else:
            if not isinstance(t, (tuple, list)) or len(t) < 3:
                raise MachineError(
                    f"Transition must be a tuple of (trigger, source, dest[, guard][, action]), "
                    f"got {t!r} (length {len(t) if isinstance(t, (tuple, list)) else 'N/A'})"
                )
            trigger, src, dst = t[0], t[1], t[2]
            guard = t[3] if len(t) > 3 else None
            action = t[4] if len(t) > 4 else None
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

    def _notify(self, trigger: str, src: str, dst: str, ctx: Dict[str, Any]) -> None:
        if self._on_transition:
            self._on_transition(trigger, src, dst, ctx)
        for obs in self._observers:
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
        Guards receive a read-only view of ctx to prevent accidental mutation."""
        frozen = MappingProxyType({**self.ctx, **kwargs})
        for tr in self._match_candidates(trigger):
            if tr.guard is None or tr.guard(frozen):
                return True
        return False

    def trigger(self, name: str, **kwargs: Any) -> str:
        """
        Fire a trigger. Returns the new state.
        Raises InvalidTransition or GuardRejected.

        Guard evaluation uses a frozen (read-only) view of ctx+kwargs.
        Context is only mutated after a guard passes. If any callback
        (on_exit, action, on_enter) raises, the entire transition rolls back.
        """
        frozen = MappingProxyType({**self.ctx, **kwargs})

        candidates = self._match_candidates(name)
        if not candidates:
            raise InvalidTransition(name, self._state)

        for tr in candidates:
            if tr.guard is not None and not tr.guard(frozen):
                continue

            # Guard passed — snapshot before mutation, then commit.
            # shallow_ctx=True swaps deepcopy for dict() — 10-50× cheaper but
            # nested mutable values are aliased (see Machine.__init__ docstring).
            old_state = self._state
            old_ctx = dict(self.ctx) if self._shallow_ctx else copy.deepcopy(self.ctx)
            self.ctx.update(kwargs)

            # Internal transition: action only, no state change, no enter/exit
            if tr.dest is None:
                try:
                    if tr.action:
                        tr.action(self.ctx)
                except Exception:
                    self.ctx.clear()
                    self.ctx.update(old_ctx)
                    raise
                self._notify(name, old_state, old_state, self.ctx)
                return self._state

            # Full transition — rollback on failure
            history_len = len(self._history)
            try:
                if old_state in self._on_exit:
                    self._on_exit[old_state](self.ctx)

                if tr.action:
                    tr.action(self.ctx)

                self._state = tr.dest
                self._push_history(old_state, old_ctx)

                if tr.dest in self._on_enter:
                    self._on_enter[tr.dest](self.ctx)
            except Exception:
                self._state = old_state
                # Only pop if history grew during this transition
                if len(self._history) > history_len:
                    self._history.pop()
                self.ctx.clear()
                self.ctx.update(old_ctx)
                raise

            # Notify outside try block — transition is committed,
            # observer failures must not trigger rollback
            self._notify(name, old_state, tr.dest, self.ctx)
            return self._state

        raise GuardRejected(name, self._state)

    def trigger_many(self, *triggers: Union[str, Tuple[str, Dict[str, Any]]]) -> str:
        """
        Fire multiple triggers in sequence. Returns final state.
        Each element is either a trigger name or (name, kwargs) tuple.
        NOT atomic: stops and raises on first failure, leaving the machine
        in the state reached by the last successful trigger.
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
            GuardRejected(reason="guard_rejected"):    edges exist but no
                                                       candidate trigger
                                                       deterministically lands
                                                       in `dest`.
        """
        if dest not in self._states:
            raise InvalidTransition(f"->{dest}", self._state, reason="unknown_state")

        state = self._state
        # Collect candidate triggers in registration order, dedup, preserving
        # explicit-before-wildcard preference.
        seen: Set[str] = set()
        explicit_triggers: List[str] = []
        wildcard_triggers: List[str] = []
        for src, trigger in self._reverse_dispatch.get(dest, ()):
            if trigger in seen:
                continue
            if src == state:
                explicit_triggers.append(trigger)
                seen.add(trigger)
            elif src == WILDCARD:
                wildcard_triggers.append(trigger)
                seen.add(trigger)

        candidates = explicit_triggers + wildcard_triggers
        if not candidates:
            raise InvalidTransition(f"->{dest}", state, reason="no_edge")

        # For each candidate trigger, predict what trigger() would actually fire
        # by walking _match_candidates in priority order with guards evaluated
        # against a frozen view. Only commit if the winning edge's dest matches.
        frozen = MappingProxyType({**self.ctx, **kwargs})
        for trigger in candidates:
            for tr in self._match_candidates(trigger):
                if tr.guard is None or tr.guard(frozen):
                    if tr.dest == dest:
                        return self.trigger(trigger, **kwargs)
                    break  # Higher-priority edge wins and goes elsewhere — skip this trigger.

        raise GuardRejected(f"->{dest}", state)

    def undo(self) -> str:
        """Revert to previous state and context. No callbacks fired.

        Restores both state and a deep copy of ctx from before the transition.
        History is a linear stack — branching (redo after undo) is not supported.
        External side effects (I/O, database writes) are NOT reverted."""
        if not self._history:
            raise MachineError("Nothing to undo")
        state, ctx_snapshot = self._history.pop()
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
        target = self._initial if state is None else state
        if target not in self._states:
            raise MachineError(f"Unknown state '{target}'")
        self._state = target
        self._history.clear()
        if clear_ctx:
            self.ctx.clear()

    # ── Observers ─────────────────────────────────────────────────────────

    def subscribe(self, callback: Callable[..., Any]) -> Callable[..., Any]:
        """Add observer. callback(trigger, src, dst, ctx). Returns callback for unsubscribe."""
        self._observers.append(callback)
        return callback

    def unsubscribe(self, callback: Callable[..., Any]) -> None:
        self._observers.remove(callback)

    # ── Serialization ─────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Serialize runtime state. Includes "initial" since 1.5.0 so that
        round-trips through from_dict() preserve reset() semantics. Older
        snapshots without "initial" remain loadable (initial falls back to
        the snapshotted state, matching pre-1.5.0 behavior)."""
        return {
            "state": self._state,
            "initial": self._initial,
            "ctx": dict(self.ctx),
            "history": [{"state": s, "ctx": dict(c)} for s, c in self._history],
        }

    def load_dict(self, data: Dict[str, Any]) -> None:
        s = data["state"]
        if not isinstance(s, str) or s not in self._states:
            raise MachineError(f"Unknown state '{s}'")

        # Restore _initial if present (since 1.5.0). Validate against declared
        # states. Absent in legacy snapshots → leave self._initial untouched.
        if "initial" in data:
            init = data["initial"]
            if not isinstance(init, str) or init not in self._states:
                raise MachineError(f"Unknown initial state '{init}'")
            self._initial = init

        raw_hist = data.get("history", [])
        history: List[Tuple[str, Dict[str, Any]]] = []
        for entry in raw_hist:
            if isinstance(entry, dict):
                # New format: {"state": "...", "ctx": {...}}
                h = entry["state"]
                if h not in self._states:
                    raise MachineError(f"Unknown state '{h}' in history")
                history.append((h, dict(entry.get("ctx", {}))))
            elif isinstance(entry, str):
                # Legacy format: plain state string (no ctx snapshot)
                if entry not in self._states:
                    raise MachineError(f"Unknown state '{entry}' in history")
                history.append((entry, {}))
            else:
                raise MachineError(f"Invalid history entry: {entry!r}")

        self._state = s
        self.ctx.clear()
        self.ctx.update(data.get("ctx", {}))
        # Truncate to history_size to prevent unbounded memory from untrusted input.
        # 1.5.0: silent truncation is deprecated. 2.0.0 will raise instead.
        if self._history_size > 0 and len(history) > self._history_size:
            warnings.warn(
                f"history truncated from {len(history)} to {self._history_size} entries; "
                f"will raise MachineError in tramoya 2.0",
                DeprecationWarning,
                stacklevel=2,
            )
            history = history[-self._history_size:]
        maxlen = self._history_size if self._history_size > 0 else None
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
        initial = data.get("initial", data["state"])
        m = cls(states=states, transitions=transitions, initial=initial, **kwargs)
        m.load_dict(data)
        return m

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(
        cls,
        states: List[str],
        transitions: List[Union[Tuple[Any, ...], _Transition]],
        json_str: str,
        **kwargs: Any,
    ) -> "Machine":
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

        Replace order matters: "&" first, otherwise the entities we insert
        below would themselves be escaped."""
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
        lines = [
            f'digraph {esc(title)} {{',
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

    def to_mermaid(self) -> str:
        """Export to Mermaid stateDiagram-v2 format.

        Trigger names are escaped via _mermaid_label() so characters like ":",
        "|", "<", ">" don't silently break the render."""
        mid = self._mermaid_id
        mlabel = self._mermaid_label
        lines = ["stateDiagram-v2"]
        for trigger, trans in self._transitions.items():
            for tr in trans:
                label = mlabel(trigger)
                if tr.guard:
                    label += " [guarded]"
                if tr.source == WILDCARD:
                    # Expand wildcard to edges from every state
                    for s in sorted(self._states):
                        dst = mid(tr.dest) if tr.dest else mid(s)
                        lines.append(f"    {mid(s)} --> {dst} : {label}")
                else:
                    src = mid(tr.source)
                    dst = mid(tr.dest) if tr.dest else src
                    lines.append(f"    {src} --> {dst} : {label}")
        return "\n".join(lines)

    def _transition_topology(self) -> Dict[str, List[Tuple[str, Optional[str], bool, bool]]]:
        """Extract transition topology for equality comparison.
        Returns {trigger: [(source, dest, has_guard, has_action), ...]}."""
        return {
            trigger: [(tr.source, tr.dest, tr.guard is not None, tr.action is not None) for tr in trans]
            for trigger, trans in self._transitions.items()
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Machine):
            return NotImplemented
        return (self._state == other._state
                and self.ctx == other.ctx
                and self._history == other._history
                and self._states == other._states
                and self._transition_topology() == other._transition_topology())

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
        self.machine.reset()
        self.machine.ctx.clear()
        if self._shared_keys:
            for key in self._shared_keys:
                if key in ctx:
                    self.machine.ctx[key] = copy.deepcopy(ctx[key])

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
        Returns composite state. Raises InvalidTransition only if
        NO region can handle the trigger.

        Note: guards are evaluated twice per region (once in can(), once in
        trigger()). Guards must be pure functions — side effects in guards
        (I/O, counters, logging) will execute twice."""
        fired = False
        for m in self._regions.values():
            if m.can(name, **kwargs):
                m.trigger(name, **kwargs)
                fired = True
        if not fired:
            states = ", ".join(f"{k}={v.state}" for k, v in self._regions.items())
            raise InvalidTransition(name, f"[{states}]")
        return self.state

    def trigger_region(self, region: str, name: str, **kwargs: Any) -> str:
        """Fire a trigger in a specific region only."""
        if region not in self._regions:
            raise MachineError(f"Unknown region '{region}'")
        return self._regions[region].trigger(name, **kwargs)

    def undo(self, region: Optional[str] = None) -> Dict[str, str]:
        """Undo last transition. If region specified, undo only that region.
        Otherwise undo ALL regions (each one step back)."""
        if region:
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
        for name, region_data in data.items():
            if name not in self._regions:
                raise MachineError(f"Unknown region '{name}' in data")
            self._regions[name].load_dict(region_data)

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
            self._actions[(trigger, source, dest)] = fn
            return fn
        return decorator

    def guard(self, trigger: str, source: str, dest: Optional[str]) -> Callable[..., Any]:
        """Decorator: register guard for a transition."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._guards[(trigger, source, dest)] = fn
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
        transitions (in registration order of first guard or action seen)."""
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
            on_enter=self._on_enter or None,
            on_exit=self._on_exit or None,
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