# Paper + Live Execution Modes Plan

## Goal
Add two supported execution modes to the bot:
- `paper` — local simulated trading with realistic lifecycle tracking
- `live` — current Bybit live trading mode

Implement in 3 phases and stop after each phase to report status before continuing.

## Why this change
The current bot is tightly coupled to live execution in `auto_trades.py`. To safely support paper trading without duplicating business logic, we should first introduce a clean execution-mode layer and explicit mode-aware lifecycle/status handling.

## Reality Check
- Current architecture already splits scanning (`main.py`) from execution (`auto_trades.py`), which is good.
- Current autotrader logic is still live-exchange oriented and websocket-driven.
- Paper mode cannot rely on exchange fills/websocket events; it must simulate entries/exits from market data.
- Existing signal lifecycle already improved (`Waiting Entry` / `Queued` / `Order Placed` / `Active` / `Closed` / `Cancelled`), which helps.
- A believable paper mode needs explicit rules for fill ordering, TP/SL ambiguity, fees, and slippage.

## Proposed Modes
- `execution.mode = paper`
- `execution.mode = live`

Optional related config for later phases:
- `execution.paper_initial_balance`
- `execution.paper_fee_rate`
- `execution.paper_slippage_bps`
- `execution.paper_fill_on_touch`
- `execution.paper_conservative_intrabar`

## Phased Implementation

### Phase 1 — Mode foundation + dry-safe paper shell
Goal: introduce execution mode support and make paper mode operational without touching live trading behavior.

#### Changes
1. Add execution config section with mode defaulting to `paper` or explicit required value.
2. Add startup logging / status visibility showing current mode clearly.
3. Refactor `auto_trades.py` so order placement flows through a mode-aware execution wrapper.
4. For `paper` mode:
   - do not call Bybit order placement
   - create local pending/virtual trades in DB
   - label notifications/dashboard/status with `[PAPER]`
5. For `live` mode:
   - preserve existing behavior
   - label notifications/dashboard/status with `[LIVE]`
6. Update `/status` output to include mode.
7. Add any schema needed for mode tagging on trades / active_trades.

#### Files likely to change
- `config.example.json`
- `modules/config_loader.py`
- `modules/notifications.py`
- `modules/control.py`
- `modules/database.py`
- `auto_trades.py`
- possibly `README.md`

#### Validation
- compileall passes
- paper mode creates virtual pending trades without hitting exchange
- live mode code path still compiles and uses current flow
- `/status` shows current mode

---

### Phase 2 — Real paper execution engine
Goal: make paper mode simulate fills and lifecycle management.

#### Changes
1. Add periodic paper execution loop in `auto_trades.py` using real market data.
2. Implement virtual entry fill rules:
   - fill on touch of entry price
   - configurable/conservative behavior
3. Implement paper trade lifecycle:
   - pending -> active
   - TP1 / TP2 / TP3 partial exits
   - breakeven SL move after TP1
   - stop loss close
4. Compute virtual realized PnL and store it.
5. Keep notifications aligned with live lifecycle, but clearly tagged `[PAPER]`.
6. Ensure live websocket logic is skipped/isolated in paper mode.

#### Simulation rules (recommended)
- Use current ticker/ohlcv checks rather than websocket fills
- Use conservative fill logic when intrabar ambiguity exists
- Support partial TP exits according to `TP_SPLIT`
- Keep status transitions synced to `trades` and `active_trades`

#### Files likely to change
- `auto_trades.py`
- `modules/database.py`
- `modules/notifications.py`
- maybe `modules/control.py`

#### Validation
- paper mode can move a virtual trade from pending -> active -> partial TP -> closed
- no exchange orders are created in paper mode
- live mode remains unchanged

---

### Phase 3 — Realism improvements
Goal: improve paper mode quality so performance numbers are more believable.

#### Changes
1. Add fee modeling on entry/exit.
2. Add slippage modeling.
3. Add better fill ordering for ambiguous bars.
4. Add richer reporting fields for paper trades:
   - gross pnl
   - fees
   - net pnl
   - fill price / exit price
5. Make dashboards/reports mode-aware.
6. Optionally add paper equity tracking in status/health.

#### Files likely to change
- `auto_trades.py`
- `modules/database.py`
- `modules/notifications.py`
- `modules/control.py`
- `README.md`

#### Validation
- paper closed trades show net pnl after fees/slippage
- daily report reflects paper results correctly
- mode labels remain obvious in all notifications

## Suggested Schema Direction
Prefer extending existing tables instead of creating a separate `paper_trades` table.

Suggested new columns:
- `trades.execution_mode`
- `active_trades.execution_mode`
- `active_trades.is_virtual`
- `active_trades.virtual_entry_price`
- `active_trades.virtual_exit_price`
- `active_trades.realized_fees`
- `active_trades.realized_pnl_net`
- `active_trades.realized_pnl_gross`
- `active_trades.remaining_quantity`

This keeps lifecycle logic unified and avoids duplicated reporting code.

## Risks / Tradeoffs
1. Paper mode can look unrealistically good if fill rules are too generous.
2. Mixing live and paper trades in the same tables requires explicit mode filtering in reports/dashboard.
3. Websocket logic must not run in paper mode where it has no meaning.
4. Partial TP simulation needs careful quantity accounting.

## Reporting Workflow
Per user request, implementation should stop after each phase and report back before moving on:
1. complete Phase 1 -> report
2. wait/continue -> complete Phase 2 -> report
3. wait/continue -> complete Phase 3 -> report

## Recommended next action
Begin with Phase 1 only.
