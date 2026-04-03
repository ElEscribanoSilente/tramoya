# Changelog

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
