# Changelog

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
