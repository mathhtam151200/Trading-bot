# Polymarket Trading Bot

A multi-agent trading bot for Polymarket prediction markets.

## Architecture

```
Scanner → Analyzer (Claude) → Risk Manager → Executor → Monitor → Postmortem
```

| Agent | Role |
|---|---|
| **Scanner** | Scans 300+ markets via Gamma API, detects arbitrage & mispricing |
| **Analyzer** | Uses Claude + news to estimate true probability vs market price |
| **Risk Manager** | Kelly Criterion sizing, hard position caps, portfolio exposure limits |
| **Executor** | Places trades via CLOB API (paper or live) |
| **Monitor** | Tracks open positions, takes profit, detects resolution |
| **Postmortem** | Analyzes every loss with Claude, saves lessons to improve future trades |

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env with your keys
```

### 3. Get your keys
- **Anthropic API key**: https://console.anthropic.com
- **Polymarket wallet**: Create on https://polymarket.com (only needed for live trading)
- **Telegram bot**: Create via @BotFather on Telegram (optional, for notifications)

## Usage

### Phase 1 — Paper Trading (START HERE)
```bash
# Run in paper mode — no real money, learn the market
python main.py --paper
```

Run for **at least 1-2 weeks** before going live. Check:
- Win rate > 55%
- Claude's probability estimates are well-calibrated
- Strategy is consistently finding edge

### Phase 2 — Live Trading
```bash
# After validating in paper mode
# Set PAPER_TRADING=false in .env
python main.py
```

### Other commands
```bash
python main.py --scan       # Single scan, see what markets are available
python main.py --report     # Performance report
python main.py --postmortem # Run postmortems on unanalyzed losses
```

## Strategy

### 1. Arbitrage (Risk-Free)
When YES + NO prices sum to less than $1.00:
```
YES = 0.45, NO = 0.50 → total = 0.95
Buy both → guaranteed $1.00 at resolution → 5.26% profit
```

### 2. Mispricing Detection
Claude estimates true probability using news + reasoning.
If market price differs by >8% from our estimate and confidence >65%, we trade.

```
Market: "Will X happen?" → YES = 0.28
Claude estimate: 0.68 probability
Edge = 0.68 - 0.28 = 0.40 → Strong BUY signal
```

### 3. Risk Management
- **Kelly Criterion** (quarter Kelly): Mathematically optimal position sizing
- **Max 10% of capital** per trade (hard cap)
- **Max 50% of capital** in open positions simultaneously
- **Min $2** per trade (to cover gas fees)

## Safety Rules

1. **Always start in paper mode** — validate before using real money
2. **200€ starting capital** — you can only lose what you put in
3. **Never disable the risk manager** — it protects your capital
4. **Monitor daily** — prediction markets can move fast on news

## File Structure

```
├── config/
│   └── settings.py         # All configuration
├── core/
│   ├── scanner.py          # Gamma API market scanner
│   ├── analyzer.py         # Claude probability estimator
│   ├── risk_manager.py     # Kelly criterion + position limits
│   ├── executor.py         # CLOB API trade execution
│   ├── monitor.py          # Position monitoring
│   └── postmortem.py       # Loss analysis
├── utils/
│   ├── database.py         # SQLite trade history
│   ├── telegram.py         # Telegram notifications
│   └── logger.py           # Colored logging
├── tests/
│   └── paper_trading.py    # Standalone paper trading simulator
├── data/
│   ├── db/trades.db        # Trade history (auto-created)
│   └── logs/bot.log        # Log file (auto-created)
├── main.py                 # Entry point
└── .env                    # Your configuration (never commit this)
```
