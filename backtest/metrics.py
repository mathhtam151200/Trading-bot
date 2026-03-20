"""
Performance metrics used by prop traders to evaluate strategies.

Metrics:
  - Total return
  - Win rate
  - Profit factor  (gross profit / gross loss)
  - Sharpe ratio   (risk-adjusted return, annualized)
  - Max drawdown   (worst peak-to-trough loss)
  - Kelly fraction (optimal bet size given historical win rate / odds)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class TradeResult:
    pnl_pct: float          # PnL as fraction of position cost (e.g. +0.43 = +43%)
    cost_usdc: float        # Capital deployed
    won: bool


@dataclass
class BacktestMetrics:
    strategy_name: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl_usdc: float
    total_cost_usdc: float
    win_rate: float
    profit_factor: float
    sharpe_ratio: Optional[float]
    max_drawdown_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    expected_value_per_trade: float
    final_capital: float
    initial_capital: float

    def __str__(self) -> str:
        lines = [
            f"\n{'='*55}",
            f"  STRATEGY: {self.strategy_name}",
            f"{'='*55}",
            f"  Trades        : {self.total_trades}",
            f"  Win rate      : {self.win_rate:.1%}  ({self.winning_trades}W / {self.losing_trades}L)",
            f"  Profit factor : {self.profit_factor:.2f}  (>1.6 = good)",
            f"  Sharpe ratio  : {self.sharpe_ratio:.2f}  (>1.0 = good)" if self.sharpe_ratio else "  Sharpe ratio  : N/A",
            f"  Max drawdown  : {self.max_drawdown_pct:.1%}",
            f"  Avg win       : {self.avg_win_pct:.1%} per trade",
            f"  Avg loss      : {self.avg_loss_pct:.1%} per trade",
            f"  EV / trade    : {self.expected_value_per_trade:+.4f}",
            f"  Capital       : ${self.initial_capital:.2f} → ${self.final_capital:.2f}",
            f"  Total PnL     : ${self.total_pnl_usdc:+.2f}  ({(self.final_capital/self.initial_capital - 1):.1%})",
            f"{'='*55}",
        ]
        return "\n".join(lines)

    @property
    def is_viable(self) -> bool:
        """Strategy is worth pursuing if it passes these minimum thresholds."""
        return (
            self.profit_factor > 1.3
            and self.win_rate > 0.45
            and self.max_drawdown_pct < 0.40
            and self.total_trades >= 30
        )


def compute_metrics(
    trades: list[TradeResult],
    strategy_name: str,
    initial_capital: float = 200.0,
    risk_free_rate: float = 0.05,  # 5% annual (approx EUR savings)
) -> BacktestMetrics:
    """Compute all performance metrics from a list of trade results."""

    if not trades:
        return BacktestMetrics(
            strategy_name=strategy_name,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            total_pnl_usdc=0,
            total_cost_usdc=0,
            win_rate=0,
            profit_factor=0,
            sharpe_ratio=None,
            max_drawdown_pct=0,
            avg_win_pct=0,
            avg_loss_pct=0,
            expected_value_per_trade=0,
            final_capital=initial_capital,
            initial_capital=initial_capital,
        )

    wins = [t for t in trades if t.won]
    losses = [t for t in trades if not t.won]

    win_rate = len(wins) / len(trades)

    gross_profit = sum(t.pnl_pct * t.cost_usdc for t in wins)
    gross_loss = abs(sum(t.pnl_pct * t.cost_usdc for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    avg_win = (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0

    # --- Equity curve (compound returns) ---
    capital = initial_capital
    equity_curve = [capital]
    for t in trades:
        pnl = t.pnl_pct * t.cost_usdc
        capital += pnl
        equity_curve.append(capital)

    final_capital = equity_curve[-1]
    total_pnl = final_capital - initial_capital

    # --- Max drawdown ---
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # --- Sharpe ratio (per-trade, annualized assuming ~200 trades/year) ---
    returns = [t.pnl_pct for t in trades]
    if len(returns) >= 5:
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        std_r = math.sqrt(variance) if variance > 0 else 0
        # Annualize: assume 200 trades per year on Polymarket
        trades_per_year = 200
        if std_r > 0:
            sharpe = (mean_r * trades_per_year - risk_free_rate) / (std_r * math.sqrt(trades_per_year))
        else:
            sharpe = None
    else:
        sharpe = None

    ev_per_trade = sum(t.pnl_pct for t in trades) / len(trades)

    return BacktestMetrics(
        strategy_name=strategy_name,
        total_trades=len(trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        total_pnl_usdc=round(total_pnl, 2),
        total_cost_usdc=round(sum(t.cost_usdc for t in trades), 2),
        win_rate=win_rate,
        profit_factor=round(profit_factor, 3),
        sharpe_ratio=round(sharpe, 2) if sharpe else None,
        max_drawdown_pct=round(max_dd, 4),
        avg_win_pct=round(avg_win, 4),
        avg_loss_pct=round(avg_loss, 4),
        expected_value_per_trade=round(ev_per_trade, 4),
        final_capital=round(final_capital, 2),
        initial_capital=initial_capital,
    )
