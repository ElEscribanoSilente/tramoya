# Changelog

## 1.6.0

The verification release, part 1: the machine audits its own topology.

### New API

- **[additive]** `Machine.lint(include_info=False)` — static analysis of the
  trigger topology, returning a list of `LintFinding` (new frozen dataclass,
  exported). Three kinds: `dead_edge` (warning — an earlier unguarded edge for
  the same trigger always wins the dispatch, so this edge can provably never
  fire; covers explicit-vs-explicit, wildcard-vs-wildcard, and wildcards dead
  because every state has its own unguarded explicit edge; `shadowed_by` names
  the culprit edge, and is `None` in that last case ("every state has its own
  unguarded explicit edge"), where no single edge is to blame),
  `unreachable_state` (warning — BFS from initial over fireable edges only;
  dead edges grant no reachability, and a wildcard locally shadowed in one
  state contributes no successor from that state), and
  `sink_state` (info, opt-in — reachable but nothing leaves it; info because
  tramoya has no "final" marker and a dead end is often intentional). Guards
  are opaque to the analysis, so warnings have zero false positives by design;
  the trade-off is under-reporting (an always-False guard is not detectable
  statically). `lint()` is pure: never raises, never mutates, deterministic
  output order (dead edges grouped by trigger in first-registration order, each
  trigger's edges in registration order; then states sorted by name).

### Bug fixes (adversarial audit 2026-10)

- **[deprecated]** Re-entrancy — calling `trigger()`, `transition_to()`,
  `undo()` or `reset()` on a machine from inside one of its own callbacks
  (`on_exit`, action, `on_enter`) — emits `DeprecationWarning` and will raise
  `MachineError` in tramoya 2.0. It was never supported, only undocumented.
  (PER-MOT-001)
- **[additive]** In the auto-transition-on-enter pattern (an `on_enter` that
  fires the next trigger), history is now chronological and a failing outer
  callback discards the undo points its nested commits recorded. (behavior
  change: history came out reversed — `undo()` moved *forward* — and a rolled
  back transition left phantom entries that `undo()` then landed on, at
  `history_size` even evicting a real entry.) Observers are notified outside
  the re-entrancy window, so chaining a transition from an observer stays
  sequential and warning-free. Cost on the non-reentrant hot path: one
  counter and one marker, O(1). (PER-MOT-001)
- **[additive]** `to_dict()` returns a real snapshot: ctx and every history
  ctx are deep copies (shallow with `shallow_ctx=True`, like every other copy
  in that mode). (behavior change: it used to return top-level copies whose
  nested values aliased the live ctx and the undo snapshots, so an in-memory
  checkpoint changed retroactively when an action mutated a list in place,
  mutating the returned dict corrupted what `undo()` restored, and
  `SubMachine.exit()`→`restore()` reloaded state from *after* the exit. A ctx
  value that cannot be deep-copied now raises `MachineError` from `to_dict()`
  instead of being silently aliased; `to_json()` is unaffected — it
  serializes a read-only view and pays no copy.) Cost: `to_dict()` in the
  default mode is now O(size of ctx × history entries) — measured ~40 ms for
  50 history entries × 200-item ctx, versus microseconds for the old shallow
  copy; take in-memory checkpoints sparingly or use `shallow_ctx=True`.
  (PER-SER-002)
- **[additive]** Docs: `trigger()`, `can()` and `subscribe()` now state the
  real scope of the guarantees — the rollback restores state/ctx/history, not
  the side effects of callbacks that already ran; after a rollback or `undo()`
  ctx values are the snapshot's copies; ctx must be deep-copyable in the
  default mode; the guards' read-only view is shallow; an observer that raises
  stops the notification of the observers after it. Module Limitations list
  re-entrancy and the deep-copy requirement. (PER-DOC-001)
- **[additive]** `transition_to()` classifies a trigger as explicit when it has
  ANY explicit edge from the current state, whatever the registration order of
  its wildcard edge. (behavior change: with `("t1", "*", "d")` registered before
  `("t1", "a", "d")` and a later `("t2", "a", "d")`, it used to file `t1` under
  wildcards and fire `t2` first, contradicting the documented
  explicit-before-wildcard order.) (PER-MOT-005)
- **[additive]** `transition_to()` reports `GuardRejected(reason="no_deterministic_edge")`
  only when the winning edge actually precedes the *first* edge to the target
  in dispatch order (target edges registered after a passing edge elsewhere are
  dead by construction, and `lint()` reports them). (behavior change: when a guarded edge to the target came first and a
  later edge led elsewhere, the guard was what blocked the move, yet it was
  reported as `"no_deterministic_edge"`; it is now `"guard_rejected"`.)
  (PER-MOT-004)
- **[additive]** `Machine.from_dict()` / `from_json()` raise `MachineError` for
  a non-dict document, a missing `"state"`, or a non-string `"initial"`.
  (behavior change: these used to leak a raw `KeyError`, `AttributeError` or
  `TypeError`.) Malformed JSON still raises `json.JSONDecodeError` from
  `json.loads`, unchanged. (PER-SER-001)
- **[additive]** `InvalidTransition` and `GuardRejected` implement
  `__reduce__`, so `pickle`, `copy.copy` and `copy.deepcopy` round-trip them
  with `trigger`, `state`, `reason` and `str()` intact. (behavior change:
  rebuilding them used to raise `TypeError`, so an exception kept in `ctx`
  broke the next `trigger()`, whose snapshot deep-copies ctx.) (PER-MOT-006)
- **[additive]** A `_Transition` instance passed to `Machine` is registered as
  its own copy. (behavior change: registering the same instance twice made
  `lint()`, which keys on `id()`, report a false-positive `dead_edge`.)
  (PER-LOG-001)
- **[deprecated]** A state named `"*"` emits `DeprecationWarning`: the name is
  reserved for wildcard transitions and will raise `MachineError` in tramoya
  2.0. (PER-MOT-012)
- **[additive]** `Machine(ctx={})` binds the caller's empty dict by reference,
  like any non-empty one. (behavior change: `ctx or {}` replaced an empty dict
  with a fresh one, so the caller's reference silently diverged.) The `ctx`
  docstring now states that the machine owns and mutates the dict in place and
  that it must not be shared between machines. (PER-CTX-001)
- **[breaking-future]** Documented that the keyword names `name` (`trigger`),
  `trigger` (`can`) and `dest` (`transition_to`) collide with the positional
  parameter and raise `TypeError`; positional-only parameters are planned for
  tramoya 2.0. No code change. (PER-MOT-010)
- **[additive]** Early validation: a non-callable guard or action in a
  transition tuple, a non-callable `on_enter`/`on_exit` value or
  `on_transition`, and `subscribe()` of a non-callable now raise `MachineError`
  immediately (before, the same inputs failed later, at first dispatch).
  Falsy placeholders (`action=False`, `on_transition=None`/`False`) are still
  accepted and ignored, as before; `_Transition` instances are validated the
  same way. (PER-MOT-011)
- **[deprecated]** A transition tuple with more than 5 elements emits
  `DeprecationWarning`: the extra elements are ignored today and will raise
  `MachineError` in tramoya 2.0. (PER-MOT-011)
- **[additive]** `ParallelMachine.load_dict()` rolls a failed load back by
  putting the SAME objects back (identity-preserving snapshots of only the
  regions named in the payload) instead of reloading `to_dict()` deep copies.
  (behavior change: the rollback could itself fail on a region holding a lock
  or other un-deep-copyable ctx value, even one the payload never touched,
  leaving the composite partly loaded and masking the original exception with
  a "not deep-copyable" error; it also replaced ctx objects with clones, so
  external references into ctx went stale.) (PER-CMP-006)
- **[additive]** `Machine.load_dict()` honors `shallow_ctx=True`: ctx and each
  history entry's ctx are copied with `dict()`, like every other copy in that
  mode, so `SubMachine.restore()` works with an inner machine whose ctx holds
  locks or handles. `SubMachine.enter()` assigns `shared_keys` values by
  reference when the inner machine is `shallow_ctx=True`; otherwise it deep-copies
  them and raises `MachineError("shared key 'k' is not deep-copyable: ...")`
  instead of a raw `TypeError`. (behavior change for `shallow_ctx=True`
  machines: `load_dict` now copies shallowly, consistent with the rest of that
  mode; CHANGELOG 1.5.1 said it deep-copied regardless.) (PER-SUB-001)
- **[additive]** Documented that `ParallelMachine.trigger()` is not atomic
  across regions: regions fire in insertion order and each commits on its own,
  so if a region's callback raises, the regions fired before it stay committed
  and the exception propagates (same contract as `trigger_many`). No code
  change. (PER-CMP-004)
- **[additive]** `ParallelMachine.trigger()` raises `GuardRejected` (reason
  `"guard_rejected"`) when no region fires but at least one region has the
  trigger available; `InvalidTransition` is kept for a trigger no region has.
  `ParallelMachine.undo(region="")` now raises `MachineError` (unknown region)
  instead of silently undoing every region; only `region=None` means "all".
  (behavior change: a guard-blocked trigger used to raise
  `InvalidTransition(reason="no_edge")` although the edge existed, and
  `region=""` was treated like `None`; `InvalidTransition` and `GuardRejected`
  are both `MachineError`, so `except MachineError` is unaffected.)
  (PER-CMP-005)
- **[additive]** `MachineBuilder.build()` gives the machine its own copy of the
  `@enter`/`@exit` hook dicts. (behavior change: the machine aliased the
  builder's dicts, so a hook registered after `build()` silently replaced or
  added to the hooks of the machine already built.) (PER-BLD-001)
- **[deprecated]** Registering a second `@guard` (or `@on` action) for the same
  `(trigger, source, dest)` edge emits `DeprecationWarning`: the last one wins
  today and will raise `MachineError` in tramoya 2.0. (PER-BLD-002)
- **[additive]** `Machine.__eq__` also compares `initial`. (behavior change:
  machines that differed only in their initial state, e.g. after
  `reset(other_state)`, compared equal although `reset()` takes them to
  different states.) (PER-EQU-001)
- **[additive]** `Machine.load_dict()` validates every history entry first but
  deep-copies only the entries it keeps. (behavior change: entries beyond
  `history_size`, or every entry when `history_size=0`, were deep-copied and
  then discarded, so an un-copyable value in a dropped entry raised
  `MachineError` and a large dropped history cost a full deep copy; the
  validation, the truncation `DeprecationWarning` and the `history_size == 0`
  semantics are unchanged.) (PER-SER-003)
- **[additive]** Documented that `SubMachine` state is not part of the parent's
  `to_dict()`/`load_dict()` snapshot: persist the inner machine separately
  (planned for 1.8). Module `Limitations` and `SubMachine` docstring. No code
  change. (PER-CMP-003)
- **[additive]** Documented the exact `shared_keys` contract of `SubMachine`:
  copied parent->child on `enter()` only (deep copy by default, by reference
  with `shallow_ctx=True`); nothing is copied back on `exit()`, and `restore()`
  reloads the inner ctx exactly as saved at `exit()`, shared keys included. No
  code change. (PER-CMP-002)
- **[additive]** Docs: README examples now show the real `to_json()` output
  (`"initial"` included, history as `{"state", "ctx"}` objects) and explain that
  legacy snapshots with string history still load but `undo()` restores an
  empty ctx for them; the DOT example shows the quoted `digraph "order_flow"`
  header; undo restores state AND ctx; the API tables list `transition_to`,
  `trigger_many`, `subscribe`/`unsubscribe`, `is_stuck`, `to_mermaid`,
  `from_dict`, `from_json`, `initial` and `states`. `load_dict`'s docstring now
  says `_deepcopy_ctx` turns a deep-copy `RecursionError` into `MachineError`
  (only `from_json` propagates `json.loads`' own), and `to_json` documents that
  ctx must be JSON-native (non-string keys become strings, tuples become lists,
  NaN is emitted as the non-standard literal, non-serializable values raise
  `TypeError`). The 1.6.0 `lint()` entry now states its output order and that
  `shadowed_by` is `None` when every state has its own unguarded explicit edge.
  (PER-DOC-002)
- **[additive]** `to_mermaid()` normalizes every line separator (CR, CRLF, VT,
  FF, NEL, LS, PS) in trigger and state names to `<br/>`. (behavior change: only
  `\n` was converted, so a bare `\r` or a Unicode separator in a name split the
  edge line and could inject a second statement into the diagram.)
  (PER-EXP-001)

### Examples

- `examples/ejemplo_avanzado.py`: the `volver` edge from `combate` to `cueva`
  could never fire — shadowed by the unguarded edge to `bosque` registered
  first, so "returning" from a cave fight silently landed in the forest.
  Found by `Machine.lint()` on its first run over this repo's own examples;
  fixed with origin-tracking guards. The bug predates lint — the demo script
  only worked by accident.

## 1.5.3

Minor fixes and doc clarifications from the 2026-07 adversarial audit (nitpick
tier). Code fixes ship with regression anchors.

### Bug fixes

- **[additive]** `trigger()`/`transition_to()` roll back on ANY exception,
  including `BaseException` (e.g. `KeyboardInterrupt`): ctx and state are
  restored before it propagates. Previously only `Exception` was caught, so a
  `KeyboardInterrupt` mid-callback left ctx half-mutated. (L3)
- **[additive]** `transition_to()` raises
  `GuardRejected(reason="no_deterministic_edge")` when a higher-priority edge
  shadows the path to the target (no guard involved), instead of the misleading
  `"guard_rejected"`. Genuine guard blocks still report `"guard_rejected"`. (L4)
- **[additive]** `unsubscribe()` is idempotent — a no-op instead of raising
  `ValueError` when the callback isn't subscribed. (L6)

### Internal

- **[additive]** Removed a redundant `ctx.clear()` in `SubMachine.enter()`
  (`reset()` already clears ctx). No behavior change. (L8)

### Documentation

- Clarified: internal transitions (dest=None) apply kwargs but leave no undo
  point (L1); `trigger()`/`undo()` rollback does not restore nested ctx values
  under `shallow_ctx=True` (L2); `ParallelMachine` regions fire in insertion
  order and must be independent (L5); `MachineBuilder.build()` orders guard-only
  before action-only decorator edges (L7); `trigger_many()` may leave ctx
  partially mutated on a mid-sequence failure (L9).

## 1.5.2

Bug fixes from the 2026-07 adversarial audit (MEDIUM findings). Each ships an
executed-PoC regression anchor.

### Bug fixes

- **[additive]** `transition_to()` dispatches the selected edge with a single
  guard evaluation instead of re-evaluating via `trigger()`. A non-pure guard
  could previously return differently on the second pass and silently land in
  the wrong state. `trigger()` and `transition_to()` now share an internal
  `_execute()` helper. (M10)
- **[additive]** `_notify()` iterates a snapshot of the observer list, so an
  observer that subscribes/unsubscribes during notification can no longer skip
  or double-fire another observer. (M8)
- **[additive]** `to_dot(title)` quotes the title, so titles like
  `"order-machine"` or `"my machine"` produce valid DOT and `{`/`}` cannot
  inject graph structure. (M11)
- **[additive]** `to_mermaid()` assigns unique node ids, so distinct states that
  sanitize to the same identifier (e.g. `"a-b"` and `"a_b"`) no longer merge
  into one node; a state whose id differs from its name gets an explicit
  `state "name" as id` declaration. (M12)
- **[additive]** `load_dict()` with `history_size=0` (undo disabled) drops
  snapshot history instead of loading it unbounded from untrusted input. (M3)

### API

- **[additive]** `Machine` is hashable again (identity hash). Defining `__eq__`
  had set `__hash__` to None, making instances unusable in sets/dicts/lru_cache.
  Note: hashing is by identity while `__eq__` is by value, so two value-equal
  machines are distinct keys — use machines as identity handles, not value keys. (M6)
- **[additive]** `__eq__` also compares `history_size` and `shallow_ctx`, so a
  serialization round-trip that silently dropped these construction knobs is now
  detectable via `==`. (M5)

### Documentation

- Documented that `__eq__` compares whether edges *have* guards/actions, not the
  callables' behavior (M7); that observers run post-commit and their exceptions
  propagate without rollback (M9); that `to_dict()` does not serialize
  construction knobs (M5); and that deeply nested ctx can raise `RecursionError`
  from deep-copy / `json.loads` on untrusted snapshots (M4).

## 1.5.1

Bug fixes from the 2026-07 adversarial audit. All three were confirmed with
executed PoCs that are now regression anchors in the test suite.

### Bug fixes

- **[additive]** `trigger()` now records the undo point only after `on_enter`
  succeeds. Previously, when the history deque was at `maxlen`, a callback that
  raised during a transition corrupted the undo history — the failed
  transition's entry was retained and a legitimate one silently lost — because
  the length comparison used to decide the compensating `pop` never fired
  (an `append` at `maxlen` evicts the oldest entry without changing the length).
  Rollback is now genuinely atomic across state, ctx, and history. (A1)
- **[additive]** `load_dict()` (and `ParallelMachine.load_dict()`) is now
  atomic: the input is fully validated and deep-copied before any attribute is
  mutated. Malformed snapshots (non-dict input, missing `"state"`, non-dict
  `ctx`, non-list `history`, invalid entries, un-deep-copyable ctx) now raise
  `MachineError` instead of leaking a raw `KeyError`/`TypeError`/`AttributeError`,
  and leave the machine untouched instead of half-mutated. (A2)
- **[additive]** `load_dict()` now deep-copies nested `ctx` values (the main ctx
  and each history entry's ctx). Previously it copied only the top level, so
  mutating the input dict after loading — or reusing a parsed snapshot across
  machines — silently aliased and corrupted internal state, contradicting the
  documented transactional safety. Applies regardless of `shallow_ctx`. (A3)

### Behavior notes

- The A1 fix changes history ordering only in the undocumented, re-entrant case
  where an `on_enter` callback fires a nested `trigger()`. Non-re-entrant code is
  unaffected.
- `ParallelMachine.load_dict()` rollback relies on `deepcopy`; if a region's
  *live* ctx holds a deliberately un-deep-copyable object, a failed load can
  still leave regions partially applied (and raises `MachineError`). This cannot
  occur with JSON-derived snapshots.

## 1.5.0

Each entry is tagged: **[additive]** (no observable change for existing code),
**[deprecated]** (warns now, will change in 2.0), **[breaking-future]** (called
out for the 2.0 plan).

### Bug fixes

- **[additive]** `to_dict()` now includes `"initial"` and `from_dict()` /
  `load_dict()` restore it. `reset()` after a serialization round-trip now
  returns to the original initial state instead of the snapshotted state.
  Legacy snapshots without `"initial"` continue to load (initial falls back to
  `data["state"]`, matching pre-1.5.0 behavior).
- **[additive]** `to_mermaid()` escapes characters that silently broke the
  render (`:`, `|`, `<`, `>`, `"`, `&`, `\n`). Trigger names like `"price>50"`
  or `"foo:bar"` now render correctly. Output for triggers with only
  alphanumeric characters is unchanged.

### Performance

- **[additive]** Precomputed dispatch index in `Machine.__init__`. `trigger()`,
  `can()`, and `available_triggers` are now O(1) per lookup instead of O(M)
  scans over all transitions. No observable behavior change — same priority
  rules (explicit source beats wildcard, registration order within each).

### New API

- **[additive]** `Machine.transition_to(dest, **ctx)` — fire whichever
  registered trigger leads from the current state to `dest`. For integrating
  with code that wants to set state imperatively. Predicts what `trigger()`
  would actually fire before committing, so overloaded trigger names don't
  silently dispatch the wrong edge.
- **[additive]** `shallow_ctx=True` constructor flag. When set, ctx snapshots
  use `dict()` instead of `copy.deepcopy()` — 10-50× cheaper for large/complex
  ctx, but mutable nested values are aliased (undo and rollback won't see
  pre-mutation state for them). Default `False` preserves full transactional
  safety.
- **[additive]** `InvalidTransition.reason` and `GuardRejected.reason` —
  typed string field for programmatic dispatch on error kind. Defaults:
  `"no_edge"` for InvalidTransition, `"guard_rejected"` for GuardRejected.
  `transition_to()` may also raise `InvalidTransition(reason="unknown_state")`.
  `__str__` of both classes is unchanged — string-based parsing of error
  messages continues to work.

### Deprecations

- **[deprecated]** `load_dict()` truncating history silently when
  `len(data["history"]) > history_size` now emits a `DeprecationWarning`. In
  tramoya 2.0 this will raise `MachineError` instead.
- **[deprecated]** Passing `ctx` as a kwarg to `Machine.from_dict()` now emits
  a `DeprecationWarning` (it has always been a no-op — ctx is restored from
  `data`). In tramoya 2.0 this will raise `TypeError`.

## 1.3.0

- **Fix**: `MachineBuilder.build()` now produces deterministic transition order (no more set-based key collection)
- **Fix**: `SubMachine.enter()` clears inner ctx on re-entry (no more ghost state)
- **Fix**: Explicit transitions always beat wildcards in `_match_candidates()`
- **Fix**: Transitions are atomic — callback exceptions trigger full rollback (state, ctx, history)
- **Fix**: `undo()` now reverts both state AND context via deep-copy snapshots
- **Fix**: `to_dot()` renders internal transitions as self-loops instead of fake nodes
- **Fix**: `load_dict()` validates types, copies defensively, supports legacy + new history format
- **New**: `is_stuck(**kwargs)` — guard-aware dead-end check (unlike `is_final` which ignores guards)
- **Internal**: History entries now store `(state, ctx_snapshot)` tuples for full undo support

## 1.2.0

- **Fix**: `ctx` no longer mutated on failed transitions (`InvalidTransition` / `GuardRejected`)
- **Fix**: History no longer desyncs when hooks/actions throw exceptions
- **Fix**: `from_dict()` validates history states (consistent with `load_dict()`)
- **Fix**: `to_dict()` returns copies instead of mutable references
- **Fix**: `reset()` handles falsy state names correctly (`is None` instead of `or`)
- **Fix**: `on_enter`/`on_exit` keys validated against declared states
- **Fix**: `to_mermaid()` expands wildcards correctly (no more `[*]` misuse)
- **Fix**: Character escaping in `to_dot()` and `to_mermaid()`
- **New**: `MachineBuilder` auto-includes initial state and deduplicates
- **New**: `SubMachine.trigger()` propagation + `shared_keys` parameter
- **New**: `MachineBuilder.observe()` and `on_transition_handler()` decorators
- **New**: `Machine.__eq__()` for comparing machines
- **New**: 104 unit tests

## 1.1.0

- Wildcard transitions (`"*"` source)
- Internal transitions (`dest=None`)
- `SubMachine` for hierarchical states
- `MachineBuilder` decorator API
- `on_transition` global callback + observers (`subscribe`/`unsubscribe`)
- `trigger_many()` batch triggers
- `is_final` property
- `from_dict()` / `from_json()` class methods
- Mermaid export (`to_mermaid()`)
- Stored `_initial` for proper `reset()`

## 1.0.0

- Initial release
- `Machine` with states, transitions, guards, entry/exit hooks
- Context dict (`ctx`)
- Undo history
- JSON serialization
- Graphviz DOT export
