"""
Backtesting engine for Polymarket strategies.

Three strategies tested:

  S1 — Pure Arbitrage
       Buy YES + NO when their sum < $1.00 (guaranteed profit at resolution).
       100% win rate by definition. Tests: how often does this exist?

  S2 — Edge-based directional (market mispricing)
       Simulate what happens when we bet on the "cheap" side.
       Uses historical market prices vs actual outcomes to measure
       how often the market was meaningfully wrong.
       Tests our edge threshold (8%) and liquidity filters.

  S3 — Kelly portfolio simulation
       Full portfolio simulation applying our exact parameters:
       Kelly fraction (0.25), max position (10%), edge threshold (8%).
       Shows the equity curve and drawdown profile on 200€ capital.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from backtest.data_fetcher import HistoricalMarket
from backtest.metrics import TradeResult, BacktestMetrics, compute_metrics


# ------------------------------------------------------------------ #
#  Strategy 1: Pure Arbitrage                                          #
# ------------------------------------------------------------------ #

def backtest_arbitrage(
    markets: list[HistoricalMarket],
    capital_per_trade: float = 20.0,
    fee_pct: float = 0.000,  # Polymarket: 0% on most markets
) -> BacktestMetrics:
    """
    Detect markets where YES + NO < 1.00 and simulate buying both.
    Profit = 1 - YES_PRICE - NO_PRICE  (per dollar invested in both legs).
    """
    trades: list[TradeResult] = []

    for m in markets:
        if not m.has_arbitrage:
            continue

        spread = -m.spread  # positive = profit (e.g. 0.03 = 3% guaranteed)
        spread_after_fees = spread - (2 * fee_pct)

        if spread_after_fees <= 0:
            continue  # Fees eat the arb

        # We split capital equally between YES and NO legs
        cost_per_leg = capital_per_trade / 2
        total_cost = capital_per_trade
        pnl = spread_after_fees * total_cost  # guaranteed profit

        trades.append(TradeResult(
            pnl_pct=spread_after_fees,
            cost_usdc=total_cost,
            won=True,
        ))

    return compute_metrics(trades, "S1 — Pure Arbitrage", initial_capital=200.0)


# ------------------------------------------------------------------ #
#  Strategy 2: Edge-based directional                                  #
# ------------------------------------------------------------------ #

def backtest_edge_strategy(
    markets: list[HistoricalMarket],
    min_edge: float = 0.08,          # Our bot's MIN_EDGE_THRESHOLD
    min_liquidity: float = 5_000,    # Our bot's MIN_MARKET_LIQUIDITY
    position_size_usdc: float = 20.0,
    fee_pct: float = 0.000,
) -> BacktestMetrics:
    """
    For each resolved market, identify which side was "underpriced" by
    at least `min_edge` relative to the true outcome.

    Logic:
      - If YES_PRICE < (1 - min_edge) AND market resolves YES → bet was good
      - If NO_PRICE  < (1 - min_edge) AND market resolves NO  → bet was good

    This simulates what would happen if a perfect oracle told us the edge
    threshold was crossed — it represents the UPPER BOUND of the strategy,
    since the real bot relies on Claude's probability estimation.
    """
    trades: list[TradeResult] = []

    for m in markets:
        if m.yes_won is None:
            continue  # Unknown outcome, skip

        if not m.passes_bot_filters(min_liquidity=min_liquidity):
            continue

        # Determine if edge threshold is crossed for either side
        yes_implied_edge = 1.0 - m.yes_price   # max edge if YES wins
        no_implied_edge = 1.0 - m.no_price     # max edge if NO wins

        bet_side = None
        entry_price = 0.0

        # We can only know which side to bet with a model estimate.
        # Proxy: bet on whichever side has a price BELOW 0.5 - min_edge
        # (i.e., the market consensus disagrees with an "equal prior")
        if m.yes_price < (0.5 - min_edge) and m.yes_won:
            bet_side = "YES"
            entry_price = m.yes_price
        elif m.no_price < (0.5 - min_edge) and not m.yes_won:
            bet_side = "NO"
            entry_price = m.no_price

        if bet_side is None:
            continue

        # Simulate the trade
        shares = position_size_usdc / entry_price
        pnl_if_win = shares - position_size_usdc   # win: shares become $1 each
        pnl_if_lose = -position_size_usdc

        # Since we selected by outcome (won), this is always a win in this sim
        pnl_usdc = pnl_if_win - (fee_pct * position_size_usdc)
        pnl_pct = pnl_usdc / position_size_usdc

        trades.append(TradeResult(
            pnl_pct=pnl_pct,
            cost_usdc=position_size_usdc,
            won=True,
        ))

    # --- Add realistic losses ---
    # The above only captures "correct" bets. In reality, Claude's model
    # is not perfect — it will sometimes bet wrong. We simulate this by
    # adding a realistic error rate based on the model's expected accuracy.
    # At confidence=70%, ~30% of signals are wrong.
    error_rate = 0.30
    n_errors = int(len(trades) * error_rate / (1 - error_rate))

    avg_entry = 0.35  # typical entry price for underpriced market
    for _ in range(n_errors):
        trades.append(TradeResult(
            pnl_pct=-1.0,       # full loss when wrong
            cost_usdc=position_size_usdc,
            won=False,
        ))

    # Shuffle to mix wins/losses in time order
    random.shuffle(trades)

    return compute_metrics(trades, "S2 — Edge-based (mispricing)", initial_capital=200.0)


# ------------------------------------------------------------------ #
#  Strategy 3: Full Kelly portfolio simulation                         #
# ------------------------------------------------------------------ #

@dataclass
class KellyTrade:
    prob_win: float
    entry_price: float
    won: bool


def _kelly_size(prob_win: float, entry_price: float, fraction: float = 0.25) -> float:
    """Quarter-Kelly position size as fraction of capital."""
    if entry_price <= 0 or entry_price >= 1:
        return 0.0
    b = (1.0 - entry_price) / entry_price
    p = prob_win
    q = 1.0 - p
    kelly = (p * b - q) / b
    return max(0.0, kelly * fraction)


def backtest_kelly_simulation(
    markets: list[HistoricalMarket],
    initial_capital: float = 200.0,
    kelly_fraction: float = 0.25,
    max_position_pct: float = 0.10,
    max_position_usdc: float = 25.0,   # Hard cap: never more than $25/trade (liquidity)
    min_edge: float = 0.08,
    min_liquidity: float = 5_000,
    model_accuracy: float = 0.68,       # Claude's realistic accuracy at 65-70% confidence
    edge_estimate: float = 0.12,        # Estimated edge when signal fires (12%)
    fee_pct: float = 0.000,
    seed: int = 42,
) -> BacktestMetrics:
    """
    Full portfolio simulation using Kelly Criterion sizing.

    Models Claude's probability estimates with realistic accuracy:
    - model_accuracy = probability that Claude's edge detection is correct
    - When correct: bet on the right side and win
    - When wrong: bet on the wrong side and lose

    Hard position cap: min(10% of capital, $25 USDC)
    This reflects real Polymarket liquidity constraints — you can't
    bet $50k on a market with $10k total liquidity.

    Uses our actual bot parameters (from config/settings.py).
    """
    random.seed(seed)

    # Build candidate trades from resolved markets
    candidates: list[KellyTrade] = []
    for m in markets:
        if m.yes_won is None:
            continue
        if not m.passes_bot_filters(min_liquidity=min_liquidity):
            continue

        # Simulate Claude's signal for this market
        # When Claude fires a signal, it estimates: prob_win = price + edge_estimate
        # It's correct model_accuracy % of the time
        correct = random.random() < model_accuracy

        if correct:
            if m.yes_won:
                prob_win = m.yes_price + edge_estimate
                entry_price = m.yes_price
                won = True
            else:
                prob_win = m.no_price + edge_estimate
                entry_price = m.no_price
                won = True
        else:
            if m.yes_won:
                # Wrong: bet NO when YES wins
                prob_win = m.no_price + edge_estimate
                entry_price = m.no_price
                won = False
            else:
                # Wrong: bet YES when NO wins
                prob_win = m.yes_price + edge_estimate
                entry_price = m.yes_price
                won = False

        prob_win = min(prob_win, 0.95)
        implied_edge = prob_win - entry_price

        if implied_edge < min_edge:
            continue

        candidates.append(KellyTrade(
            prob_win=prob_win,
            entry_price=entry_price,
            won=won,
        ))

    trades: list[TradeResult] = []
    capital = initial_capital

    for kt in candidates:
        if capital < 5:
            break  # Ruin

        kelly_pct = _kelly_size(kt.prob_win, kt.entry_price, kelly_fraction)
        position_pct = min(kelly_pct, max_position_pct)
        # Apply BOTH percentage cap and absolute dollar cap
        position_usdc = min(capital * position_pct, max_position_usdc)

        if position_usdc < 2.0:
            continue

        shares = position_usdc / kt.entry_price

        if kt.won:
            pnl_usdc = (shares - position_usdc) * (1 - fee_pct)
        else:
            pnl_usdc = -position_usdc

        pnl_pct = pnl_usdc / position_usdc
        capital += pnl_usdc

        trades.append(TradeResult(
            pnl_pct=pnl_pct,
            cost_usdc=position_usdc,
            won=kt.won,
        ))

    return compute_metrics(
        trades,
        f"S3 — Kelly Portfolio (accuracy={model_accuracy:.0%})",
        initial_capital=initial_capital,
    )


# ------------------------------------------------------------------ #
#  Sensitivity analysis                                                #
# ------------------------------------------------------------------ #

def sensitivity_analysis(
    markets: list[HistoricalMarket],
    initial_capital: float = 200.0,
) -> list[dict]:
    """
    Test how robust S3 is to changes in key parameters.
    Returns a list of results for different accuracy/edge combinations.
    """
    results = []
    for accuracy in [0.55, 0.60, 0.65, 0.68, 0.72, 0.75]:
        for min_edge in [0.05, 0.08, 0.12, 0.15]:
            # edge_estimate = min_edge + 4% (we filter at min, estimate at min+4%)
            m = backtest_kelly_simulation(
                markets,
                initial_capital=initial_capital,
                model_accuracy=accuracy,
                min_edge=min_edge,
                edge_estimate=min_edge + 0.04,
            )
            results.append({
                "accuracy": accuracy,
                "min_edge": min_edge,
                "trades": m.total_trades,
                "win_rate": m.win_rate,
                "profit_factor": m.profit_factor,
                "sharpe": m.sharpe_ratio,
                "final_capital": m.final_capital,
                "max_drawdown": m.max_drawdown_pct,
                "viable": m.is_viable,
            })
    return results
