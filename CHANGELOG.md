# Changelog

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
