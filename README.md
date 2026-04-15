# Bybit Quant Bot — Extended Fork with Paper Trading, Multi-Channel Alerts, and Safer Production Controls

This repository is a heavily extended fork of the original Bot-Auto-Screening-Bybit project by hirazawa-yui99.

It keeps the original core idea:
- scan Bybit markets
- detect technical/pattern-based setups
- persist trade state in PostgreSQL
- alert the operator with rich signal output

But this fork significantly expands the project in three major directions:
1. notification flexibility
2. safer production operation
3. paper/live execution mode support

If you are reading this because you want to review changes for a future merge request, start with:
- "Differences from the Original Repository"
- "Architecture Changes in This Fork"
- "Execution Modes"
- "Notification System"

--------------------------------------------------
## 1. Upstream Reference
Original repository:
https://github.com/hirazawa-yui99/Bot-Auto-Screening-Bybit

Original README title:
- Bybit Quant Trading Bot v8.1.2

Original project emphasized:
- Bybit scanning
- PostgreSQL persistence
- Discord dashboards and webhooks
- Smart Money Concepts + pattern detection + quant filters
- production deployment via systemd/cron

This fork preserves the general strategy workflow while changing several operational and architectural pieces.

--------------------------------------------------
## 2. Project Structure and Responsibilities

Core files:
- `main.py` — market scanner entrypoint; loads markets, computes signals, writes trade rows
- `auto_trades.py` — execution entrypoint; ingests signals, places orders, reconciles fills, manages paper/live lifecycle
- `modules/config_loader.py` — config loading, validation, env overrides, execution mode enforcement
- `modules/database.py` — PostgreSQL pool, schema migration, active-signal lookup
- `modules/control.py` — pause/resume state, heartbeat tracking, health snapshot
- `modules/notifications.py` — Telegram/Discord alerts and dashboard updates
- `modules/logging_setup.py` — shared logging bootstrap and rotating file handlers
- `modules/runtime_paths.py` — repo-root anchored path helpers
- `modules/runtime_utils.py` — retry/backoff wrapper
- `modules/paper_trade_utils.py` — paper-trade math, mode normalization, validation helpers
- `modules/domain.py` — shared trade domain models (`TradeSignal`, `ActiveTrade`)
- `modules/execution_types.py` — shared order/execution primitives (`OrderIntent`, `ExecutionEvent`)
- `modules/technicals.py`, `modules/patterns.py`, `modules/smc.py`, `modules/quant.py`, `modules/derivatives.py` — strategy filters and scoring

--------------------------------------------------
## 3. What This Fork Adds
This fork adds or changes all of the following:

### A. Notification flexibility
- Telegram support added
- Discord support retained
- user can choose:
  - Telegram only
  - Discord only
  - both Telegram and Discord
- Telegram control commands added:
  - /status
  - /pause
  - /resume
  - /help

### B. Safer production behavior
- config validation improved
- retry/backoff support added around network operations
- rotating log files added
- status/heartbeat tracking added
- file-based health snapshot added
- Discord dashboard changed from repeated spam posts to message editing behavior
- dashboard/status counts improved

### C. Execution mode support
- explicit execution modes:
  - paper
  - live
- paper mode supports local simulated trading lifecycle
- live mode keeps real Bybit execution path
- all alerts/dashboard/status outputs are mode-labeled:
  - [PAPER]
  - [LIVE]

### D. Paper trading engine
Paper mode now supports:
- signal ingestion
- virtual order queueing
- simulated entry fills
- TP1 / TP2 / TP3 partial exits
- stop-loss exits
- breakeven stop move after TP1
- fee modeling
- slippage modeling
- paper equity tracking
- mode-aware reporting

### E. Testing foundation
The upstream repo had no tests.
This fork adds an initial test layer for paper-trading utility logic and a broader regression suite for execution, control, and scanner behavior.

--------------------------------------------------
## 3. Differences from the Original Repository
This section is intentionally explicit to help future code review / merge discussion.

### 3.1 Notification System — Original vs Fork
Original:
- Discord-centric
- Discord webhook workflow was the primary alerting path
- Discord dashboard was part of the design

This fork:
- replaces single-channel assumptions with a shared notification layer
- supports Telegram and Discord simultaneously
- keeps Discord as an option instead of removing it
- adds Telegram operational control commands
- unifies alert formatting through a centralized module

Files involved:
- modules/notifications.py
- modules/telegram_bot.py
- modules/discord_bot.py
- modules/control.py

Practical difference:
- operator no longer has to depend only on Discord
- can run the bot in Telegram-only workflows
- can still keep Discord for team visibility if desired

### 3.2 Dashboard Behavior — Original vs Fork
Original:
- dashboard behavior was Discord-focused
- repeated dashboard posting behavior was part of the older webhook-oriented flow

This fork:
- dashboard is mode-aware
- dashboard message editing is supported for Telegram
- Discord dashboard behavior was improved to edit a tracked message instead of spamming new messages continuously
- dashboard now clearly labels mode:
  - [PAPER] DASHBOARD
  - [LIVE] DASHBOARD

### 3.3 Execution Architecture — Original vs Fork
Original:
- primarily live-trading oriented
- assumed real exchange-side execution / monitoring behavior
- no proper paper-trading mode

This fork:
- introduces explicit execution mode handling
- separates live and paper behavior logically
- allows the full bot to be run without sending live orders
- paper mode now simulates position lifecycle using market data and local DB state

This is one of the biggest architectural changes in the fork.

## Deployment Checklist (Production & Paper)
To deploy this bot safely and correctly, ensure the following steps are met before mainnet activation:

1. **Vault Setup**: Store API keys in a secure vault (e.g., HashiCorp Vault, AWS KMS) or OS-level keychain. Never commit `.env` or `config.json`.
2. **Env-Var Template**: Use `config.example.json` as a structural guide. Load credentials exclusively at runtime via environment variables (`BOT_ENV`, `BYBIT_KEY`, `BYBIT_SECRET`, `TELEGRAM_TOKEN`, etc.).
3. **Docker Image Build**: 
   - `docker build -t bybit-bot:latest .`
   - Verify the checksum before deploying.
4. **Step-by-Step Validation (Testnet/Paper)**:
   - Run the bot with `execution.mode` set to `paper`.
   - Connect to Bybit Testnet using test API keys (set `testnet=True` in `pybit` / `ccxt`).
   - Run a simulated 24-hour loop and assert order state transitions within 5s.
   - Once metrics pass, toggle to `live` execution for mainnet.

### 3.4 Safer Operational Controls — Original vs Fork
Original:
- production-oriented, but with fewer guardrails around notification flexibility and runtime monitoring

This fork adds:
- stronger config validation
- retry/backoff helper usage
- rotating logs
- heartbeat tracking
- pause/resume state tracking
- file-based health snapshot at:
  - logs/health_status.json
- mode-aware /status control output

### 3.5 Status Lifecycle Tracking — Original vs Fork
Original:
- trade lifecycle tracking existed, but was more loosely tied to live execution flow and notification flow

This fork standardizes signal lifecycle more explicitly with states like:
- Waiting Entry
- Queued
- Order Placed
- Active
- Closed
- Cancelled

This improves:
- deduplication
- dashboard output
- reporting clarity
- mode-aware filtering

### 3.6 Database Usage — Original vs Fork
Original:
- PostgreSQL-backed trade persistence
- schema migration behavior existed

This fork keeps PostgreSQL but extends schema usage for:
- execution_mode on trades
- execution_mode on active_trades
- paper-trading bookkeeping fields such as:
  - remaining_quantity
  - filled_quantity
  - entry_fill_price
  - exit_fill_price
  - realized_fees
  - realized_pnl_gross
  - realized_pnl_net
  - tp1_hit / tp2_hit / tp3_hit
- daily_reports now also track execution mode

This makes live and paper reporting coexist more safely.

### 3.7 Testing — Original vs Fork
Original:
- no automated test suite included

This fork:
- adds tests/ directory
- adds initial paper utility tests
- adds domain/execution primitive tests
- verifies:
  - settings merge behavior
  - conservative ambiguous-candle logic
  - slippage behavior
  - fee math
  - gross pnl math
  - config validation and live-mode safety checks
  - schema migration idempotence
  - execution state machine behavior
  - shared domain/execution model validation

This is not yet a full suite, but it is a meaningful improvement over no tests at all.

--------------------------------------------------
## 4. Current Feature Set in This Fork

### Market Analysis
The fork still keeps the original style of analysis stack:
- pattern-based signal generation
- derivatives filtering
- quant scoring
- SMC support
- BTC bias filtering
- fakeout protection
- risk/reward filtering

Main related modules:
- modules/patterns.py
- modules/quant.py
- modules/derivatives.py
- modules/smc.py
- modules/technicals.py
- main.py

### Notifications
Supported notification modes:
- Telegram only
- Discord only
- Telegram + Discord

Features:
- rich signal alerts
- chart image alerts
- scan completion alerts
- trade lifecycle alerts
- dashboard updates

### Telegram Controls
Supported commands:
- /status
- /pause [reason]
- /resume
- /help

### Execution Modes
Supported:
- paper
- live

### Reporting
- PostgreSQL-backed trade and active-trade persistence
- daily reports
- mode-aware paper/live filtering
- paper equity tracking

### Logging and Monitoring
- logs/scanner.log
- logs/auto_trades.log
- logs/health_status.json

--------------------------------------------------
## 5. Repository Structure
```text
Bot-Auto-Screening-Bybit/
├── main.py                         # scanner / signal generation loop
├── auto_trades.py                  # execution engine (paper + live)
├── config.example.json
├── requirements.txt
├── README.md
├── tests/
│   ├── conftest.py
│   └── test_paper_trade_utils.py
├── modules/
│   ├── config_loader.py            # config loading + validation
│   ├── control.py                  # pause/heartbeat/status/health snapshot
│   ├── database.py                 # DB init + schema migration
│   ├── notifications.py            # unified Telegram + Discord notifications
│   ├── telegram_bot.py             # compatibility re-export
│   ├── discord_bot.py              # compatibility re-export
│   ├── runtime_utils.py            # retry/backoff helper
│   ├── paper_trade_utils.py        # tested paper-trading utility logic
│   ├── technicals.py
│   ├── quant.py
│   ├── derivatives.py
│   ├── smc.py
│   └── patterns.py
└── deploy/
    ├── bot.service
    └── auto_trades.service
```

--------------------------------------------------
## 6. Configuration
Copy config.example.json to config.json and edit it.

### 6.1 Notifications
Example:
```json
"notifications": {
  "telegram_enabled": true,
  "discord_enabled": false,
  "telegram_control_enabled": true
}
```

If Telegram is enabled, set:
- api.telegram_bot_token
- api.telegram_chat_id

If Discord is enabled, set:
- api.discord_webhook

### 6.2 Execution Mode
Example:
```json
"execution": {
  "mode": "paper",
  "paper": {
    "initial_balance": 10000,
    "fee_rate": 0.0006,
    "slippage_bps": 5,
    "fill_on_touch": true,
    "conservative_intrabar": true
  }
}
```

Supported values:
- paper
- live

Meaning:
- paper = local simulation, no live exchange order placement
- live = real Bybit execution path

### 6.3 Paper Settings
- initial_balance
  - starting paper equity used in reporting
- fee_rate
  - fee fraction applied on entry and exit
  - example: 0.0006 = 0.06%
- slippage_bps
  - adverse slippage in basis points
- fill_on_touch
  - whether touching entry price fills the virtual order
- conservative_intrabar
  - if one candle touches both TP and SL, prefer the worse outcome

### 6.4 Live Mode Requirements
Live mode requires:
- api.bybit_key
- api.bybit_secret

Paper mode does not require live credentials to run the execution engine logic.

### 6.5 PostgreSQL Setup (WSL / Ubuntu)
This bot uses PostgreSQL for trade state and lifecycle tracking. If you are on WSL or a fresh Ubuntu install, do this first:

1. Install PostgreSQL and the client tools:
```bash
sudo apt update
sudo apt install postgresql postgresql-contrib -y
```

2. Start the PostgreSQL service:
```bash
sudo service postgresql start
```

3. Check that it is running:
```bash
sudo service postgresql status
```

4. Create a database and user:
```bash
sudo -u postgres psql
```
Then run inside psql:
```sql
CREATE DATABASE trading_bot;
CREATE USER botuser WITH PASSWORD 'yourpassword';
ALTER ROLE botuser SET client_encoding TO 'utf8';
ALTER ROLE botuser SET default_transaction_isolation TO 'read committed';
ALTER ROLE botuser SET timezone TO 'UTC';
GRANT ALL PRIVILEGES ON DATABASE trading_bot TO botuser;
\q
```

5. Put those values into `config.json`:
- `database.host`: usually `localhost`
- `database.database`: `trading_bot`
- `database.user`: `botuser`
- `database.password`: the password you created
- `database.port`: `5432`

6. Restart the bot after saving the config.

If PostgreSQL is already installed, you can skip the database creation step and just make sure the service is running and the credentials match.

--------------------------------------------------
## 7. Installation
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

### 7.1 Zero-Programming-Experience Quick Start
If you have never used a terminal or edited a config file before, follow these steps exactly:

1. Open a terminal in the project folder.
2. Create the virtual environment:
```bash
python3 -m venv venv
```
3. Turn it on:
```bash
source venv/bin/activate
```
4. Install the required packages:
```bash
pip install -r requirements.txt
```
5. Make your personal config file:
```bash
cp config.example.json config.json
```
6. Open config.json in a text editor.
7. Set execution.mode to paper first.
8. Fill in the notification and database values you want to use.
9. Save the file.
10. Start the scanner in one terminal:
```bash
python main.py
```
11. Start execution in another terminal:
```bash
python auto_trades.py
```
12. Watch the logs folder for errors or health updates.

Note:
- If you are unsure what to change in config.json, keep paper mode on and only use test credentials until everything looks correct.
- Do not switch to live mode until paper mode works for several hours without errors.

### 7.2 Beginner Config Checklist
Fill in only these fields first:
- api.telegram_bot_token: your Telegram bot token
- api.telegram_chat_id: your Telegram chat ID
- api.discord_webhook: only if you want Discord alerts
- database.host: your PostgreSQL host
- database.database: your database name
- database.user: your database username
- database.password: your database password
- database.port: usually 5432
- execution.mode: paper

Leave the rest alone unless you know why you are changing it.

--------------------------------------------------
## 8. Running the Bot
### Scanner
```bash
source venv/bin/activate
python main.py
```

### Execution Engine
```bash
source venv/bin/activate
python auto_trades.py
```

Recommended:
- start in paper mode first
- verify alerts, dashboard, and reporting
- only switch to live after validating behavior

--------------------------------------------------
## 9. systemd Deployment
Create a dedicated user first:
```bash
sudo useradd --system --home /opt/Bot-Auto-Screening-Bybit --shell /usr/sbin/nologin bybitbot
sudo chown -R bybitbot:bybitbot /opt/Bot-Auto-Screening-Bybit
```

Install services:
```bash
sudo cp deploy/bot.service /etc/systemd/system/bybit-bot.service
sudo cp deploy/auto_trades.service /etc/systemd/system/bybit-auto-trades.service
sudo systemctl daemon-reload
sudo systemctl enable bybit-bot bybit-auto-trades
sudo systemctl start bybit-bot bybit-auto-trades
```

--------------------------------------------------
## 10. Logs and Health Files
Logs:
- logs/scanner.log
- logs/auto_trades.log

Health snapshot:
- logs/health_status.json

The health file includes:
- current mode
- paused state
- active signals count
- active positions count
- scanner heartbeat
- autotrader heartbeat
- overall healthy flag

--------------------------------------------------
## 11. How Paper Mode Works in This Fork
Paper mode is not just “skip order placement.”
It is a local simulated execution engine.

Current paper lifecycle:
1. signal is generated
2. signal is stored in trades
3. autotrader ingests signal into active_trades
4. virtual order is queued
5. entry is filled when price touches entry criteria
6. TP1 / TP2 / TP3 are processed with partial exits
7. stop loss can close remaining size
8. TP1 can move stop to breakeven
9. fees and slippage are applied
10. realized net PnL updates paper equity and reports

Important caveat:
- paper mode is candle-driven, not tick-driven
- conservative intrabar logic is used to reduce optimistic results

--------------------------------------------------
## 12. Merge-Oriented Summary of Changes
If preparing a future merge request against upstream, the major review buckets are:

### Bucket 1: Multi-channel notifications
- Telegram support added
- Discord retained
- unified notification module introduced
- Telegram control commands introduced

### Bucket 2: Execution architecture
- explicit paper/live modes added
- paper execution engine implemented
- live flow preserved

### Bucket 3: Production hardening
- config validation improved
- retry/backoff helpers used
- rotating logs added
- health snapshot added
- dashboard behavior improved

### Bucket 4: Database/state management
- execution_mode tracking added
- paper-specific trade bookkeeping fields added
- reporting made mode-aware
- signal lifecycle made more explicit

### Bucket 5: Testability
- tests directory added
- utility-level paper trading tests added
- paper utility logic extracted into a dedicated module

--------------------------------------------------
## 13. Known Remaining Gaps / Future Work
This fork is much more capable than upstream operationally, but it is not “finished forever.”

Still recommended for future work:
- end-to-end tests for DB-backed paper lifecycle
- explicit /positions command
- explicit /pnl command
- startup self-check output for mode / DB / notification readiness
- cleaner removal of legacy bridge use of order_id as a temporary mode marker
- more advanced intrabar execution modeling if needed

--------------------------------------------------
## 14. Disclaimer
This software is for educational and operational experimentation purposes only.
Cryptocurrency trading carries substantial financial risk.

Paper mode is only a simulation.
Live mode can place real exchange orders.
Always verify your configuration and execution mode before running in production.
