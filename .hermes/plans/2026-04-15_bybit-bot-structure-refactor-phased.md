# Bybit Bot Structure Refactor Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Refactor the trading bot into smaller, safer, testable modules without changing live/paper behavior.

**Architecture:** Keep the current business logic intact, but progressively extract config, domain models, scanner orchestration, execution orchestration, and observability into separate modules. Each phase should be independently shippable and verified with tests before moving on.

**Tech Stack:** Python 3.11, ccxt, pybit, psycopg2, pandas, pandas_ta, schedule, pytest, hypothesis.

---

## Phase 0: Baseline and Safety Net

**Objective:** Freeze current behavior with tests and deployment checks before moving code around.

**Files:**
- Modify: `tests/test_strategy_math.py`
- Modify: `tests/test_paper_trade_utils.py`
- Create: `tests/test_config_loader.py`
- Create: `tests/test_database_schema.py`
- Create: `tests/test_execution_state_machine.py`
- Modify: `.github/workflows/ci.yml`

**Tasks:**
1. Add tests for config validation, execution mode selection, and required env handling.
2. Add tests for active-trade state transitions in paper mode.
3. Add a schema migration idempotence test for `trades`, `active_trades`, and `daily_reports`.
4. Add CI coverage threshold for the execution and strategy modules.

**Exit criteria:** Current behavior is covered well enough to refactor safely.

**Status:** Completed.

---

## Phase 1: Configuration, Paths, and Logging Unification

**Objective:** Centralize runtime config, filesystem paths, and logging setup.

**Files:**
- Modify: `modules/config_loader.py`
- Modify: `modules/control.py`
- Modify: `modules/notifications.py`
- Modify: `main.py`
- Modify: `auto_trades.py`
- Create: `modules/runtime_paths.py`
- Create: `modules/logging_setup.py`

**Tasks:**
1. Create a single path helper for logs, health snapshots, and config resolution.
2. Replace relative path usage with centralized path helpers.
3. Replace `print()` startup messages with logger calls.
4. Make live mode fail closed if secrets are missing from the environment.
5. Standardize structured logging across scanner and executor.

**Exit criteria:** All runtime paths resolve consistently, and startup/errors log in one format.

---

## Phase 2: Shared Domain Models and Execution Primitives

**Objective:** Remove duplicated trade/order/state logic by introducing shared models.

**Files:**
- Create: `modules/domain.py`
- Create: `modules/execution_types.py`
- Modify: `modules/paper_trade_utils.py`
- Modify: `modules/database.py`
- Modify: `auto_trades.py`

**Tasks:**
1. Add dataclasses or typed dicts for `TradeSignal`, `ActiveTrade`, `OrderIntent`, and `ExecutionEvent`.
2. Move side normalization, quantity validation, and price validation into shared helpers.
3. Make paper and live engines consume the same domain objects.
4. Add guardrails for zero/invalid quantities, invalid precision, and unsupported order states.

**Exit criteria:** Scanner and executor no longer rely on ad hoc dict shapes for core trade state.

---

## Phase 3: Scanner Decomposition

**Objective:** Split market scanning from strategy evaluation and alert emission.

**Files:**
- Create: `scanner/__init__.py`
- Create: `scanner/market_scan.py`
- Create: `scanner/signal_builder.py`
- Create: `scanner/scanner_scheduler.py`
- Modify: `main.py`
- Modify: `modules/technicals.py`
- Modify: `modules/patterns.py`
- Modify: `modules/smc.py`
- Modify: `modules/quant.py`
- Modify: `modules/derivatives.py`

**Tasks:**
1. Move the orchestration loop out of `main.py` into `scanner/market_scan.py`.
2. Make `signal_builder.py` own the “signal → filter → score → return result” flow.
3. Keep alert formatting and delivery separate from signal generation.
4. Add tests for symbol filtering, BTC bias gating, and rejection paths.

**Exit criteria:** `main.py` becomes a thin entrypoint, and scanner logic is reusable in tests.

---

## Phase 4: Execution Engine Decomposition

**Objective:** Split live execution, paper execution, and websocket reconciliation into dedicated modules.

**Files:**
- Create: `execution/__init__.py`
- Create: `execution/order_manager.py`
- Create: `execution/websocket_handlers.py`
- Create: `execution/paper_engine.py`
- Create: `execution/reporting.py`
- Modify: `auto_trades.py`
- Modify: `modules/notifications.py`
- Modify: `modules/control.py`
- Modify: `modules/database.py`

**Tasks:**
1. Move order placement and TP/SL submission into `order_manager.py`.
2. Move websocket callbacks into `websocket_handlers.py`.
3. Move paper-trade lifecycle simulation into `paper_engine.py`.
4. Move daily report generation into `reporting.py`.
5. Keep `auto_trades.py` as a thin bootstrap/scheduler only.

**Exit criteria:** Exchange calls, paper simulation, and websocket reconciliation are isolated and testable.

---

## Phase 5: Error Handling, Idempotency, and Concurrency Hardening

**Objective:** Make retries, locks, and state transitions deterministic.

**Files:**
- Modify: `modules/runtime_utils.py`
- Modify: `execution/order_manager.py`
- Modify: `execution/websocket_handlers.py`
- Modify: `execution/paper_engine.py`
- Modify: `scanner/market_scan.py`
- Modify: `auto_trades.py`

**Tasks:**
1. Enforce idempotency keys on live order submissions.
2. Make job scheduling explicitly non-overlapping with lock-safe failure handling.
3. Ensure DB state transitions are atomic and mode-scoped.
4. Harden websocket and polling reconciliation for duplicate, delayed, or missing events.
5. Add tests for retry failures, duplicate event handling, and mode isolation.

**Exit criteria:** Duplicate orders, stale state, and race conditions are significantly reduced.

---

## Phase 6: Observability and Operational Controls

**Objective:** Improve safe operation in production.

**Files:**
- Modify: `modules/control.py`
- Modify: `modules/notifications.py`
- Modify: `auto_trades.py`
- Modify: `main.py`
- Create: `modules/health.py`

**Tasks:**
1. Move health snapshot logic into a dedicated health module.
2. Add richer heartbeat metadata for scanner and executor.
3. Make pause/resume checks uniform across all loops.
4. Ensure critical failures are visible in logs and alerts.

**Exit criteria:** Operators can tell whether the bot is healthy, paused, degraded, or stuck.

---

## Phase 7: Test Expansion and CI Enforcement

**Objective:** Prevent regressions after the refactor.

**Files:**
- Modify: `tests/conftest.py`
- Add: `tests/scanner/`
- Add: `tests/execution/`
- Add: `tests/integration/`
- Modify: `.github/workflows/ci.yml`

**Tasks:**
1. Add unit tests for all new scanner and execution modules.
2. Add integration tests for paper-mode lifecycle and daily reporting.
3. Add websocket payload fixture tests.
4. Raise coverage gates once the refactor stabilizes.

**Exit criteria:** Core behavior is covered by tests, and CI blocks obvious regressions.

---

## Phase 8: Cleanup and Documentation

**Objective:** Finish the refactor with updated docs and deployment notes.

**Files:**
- Modify: `README.md`
- Modify: `deploy/bot.service`
- Modify: `deploy/auto_trades.service`
- Modify: `deploy/restart_bot.sh`
- Create: `docs/architecture.md`

**Tasks:**
1. Update startup commands and module layout in the README.
2. Document paper/live execution flow and where each responsibility lives.
3. Update systemd units for the new entrypoints if needed.
4. Record rollback and recovery steps.

**Exit criteria:** A new maintainer can understand, run, and recover the bot without reverse engineering it.

---

## Recommended Order of Execution

1. Phase 0 — baseline tests
2. Phase 1 — config/path/logging
3. Phase 2 — domain models
4. Phase 3 — scanner split
5. Phase 4 — execution split
6. Phase 5 — concurrency/idempotency hardening
7. Phase 6 — observability
8. Phase 7 — tests/CI
9. Phase 8 — docs/deployment cleanup

## Notes

- Keep each phase shippable on its own.
- Do not refactor scanner and executor at the same time.
- Preserve current trade state schemas until the new modules are stable.
- Prefer extraction over behavior changes.
