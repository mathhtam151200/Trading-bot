"""
Polymarket Strategy Backtester
===============================

Usage:
    python -m backtest.run_backtest            # Full backtest (fetches data)
    python -m backtest.run_backtest --nocache  # Force fresh data from API
    python -m backtest.run_backtest --quick    # Skip sensitivity analysis

What it does:
    1. Fetches 1000 resolved Polymarket markets (cached locally)
    2. Runs 3 strategies:
       S1 — Pure arbitrage (YES+NO < $1)
       S2 — Edge-based directional (market mispricing)
       S3 — Kelly portfolio simulation (full bot simulation)
    3. Prints performance metrics for each
    4. Runs sensitivity analysis (how robust is the edge?)
    5. Prints an honest final verdict
"""
from __future__ import annotations

import argparse
import sys
import os

# Allow running from project root: python -m backtest.run_backtest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.data_fetcher import fetch_resolved_markets
from backtest.engine import (
    backtest_arbitrage,
    backtest_edge_strategy,
    backtest_kelly_simulation,
    sensitivity_analysis,
)


def print_header(title: str):
    print(f"\n{'#'*60}")
    print(f"#  {title}")
    print(f"{'#'*60}")


def print_sensitivity_table(results: list[dict]):
    print("\n  SENSITIVITY ANALYSIS — S3 Kelly Portfolio")
    print(f"  {'Accuracy':>10} {'MinEdge':>8} {'Trades':>7} {'WinRate':>8} {'PF':>6} {'Sharpe':>7} {'Capital':>9} {'MaxDD':>7} {'Viable':>7}")
    print("  " + "-"*75)
    for r in results:
        sharpe_str = f"{r['sharpe']:.2f}" if r['sharpe'] else "  N/A"
        print(
            f"  {r['accuracy']:>10.0%} {r['min_edge']:>8.0%} "
            f"{r['trades']:>7} {r['win_rate']:>8.1%} "
            f"{r['profit_factor']:>6.2f} {sharpe_str:>7} "
            f"${r['final_capital']:>8.2f} {r['max_drawdown']:>7.1%} "
            f"{'YES' if r['viable'] else 'NO':>7}"
        )


def print_verdict(s1, s2, s3, markets):
    print_header("HONEST VERDICT")

    n_markets = len(markets)
    n_with_outcome = sum(1 for m in markets if m.yes_won is not None)
    n_arb = sum(1 for m in markets if m.has_arbitrage)
    n_liquid = sum(1 for m in markets if m.passes_bot_filters())

    print(f"""
  DATA QUALITY
  ─────────────────────────────────────────────────
  Markets analyzed      : {n_markets}
  Markets with outcome  : {n_with_outcome}
  Markets with arb opp  : {n_arb}  ({n_arb/n_markets:.1%} of total)
  Markets passing filter: {n_liquid}  (liquidity > $5,000)

  STRATEGY SUMMARY
  ─────────────────────────────────────────────────
  S1 Arbitrage   | PF={s1.profit_factor:.2f} | Trades={s1.total_trades} |
                 | These are real zero-risk opportunities.
                 | {'Viable ✓' if s1.total_trades > 0 else 'No arb found in sample ✗'}

  S2 Edge-based  | PF={s2.profit_factor:.2f} | WR={s2.win_rate:.1%} | Trades={s2.total_trades}
                 | Upper-bound: assumes we always identify the right direction.
                 | {'Viable ✓' if s2.is_viable else 'Not viable ✗ — need better model accuracy'}

  S3 Kelly sim   | PF={s3.profit_factor:.2f} | WR={s3.win_rate:.1%} | Trades={s3.total_trades}
                 | Realistic: Claude at 68% accuracy, 25% Kelly, max 10% per trade.
                 | {'Viable ✓' if s3.is_viable else 'Marginal ✗ — adjust parameters below'}

  RECOMMENDED NEXT STEPS
  ─────────────────────────────────────────────────""")

    if s3.is_viable:
        print("""  ✓ Strategy shows positive EV — proceed to paper trading phase.
    Run: python main.py --paper  (PAPER_TRADING=true in .env)
    Monitor for 2 weeks minimum before any live capital.
    Minimum sample: 30 trades before drawing conclusions.""")
    else:
        print("""  ⚠  Strategy is marginal — do NOT deploy live capital yet.
    Options:
      A) Raise MIN_EDGE_THRESHOLD from 8% → 12%  (fewer trades, better quality)
      B) Raise MIN_MARKET_LIQUIDITY from $5k → $10k  (better execution)
      C) Paper trade for 3 weeks and measure Claude's actual accuracy first.
    Then re-run this backtest with updated parameters.""")

    print(f"""
  KEY RISK FACTORS
  ─────────────────────────────────────────────────
  - Max drawdown simulated : {s3.max_drawdown_pct:.1%} (if > 40%, reduce Kelly fraction)
  - Polymarket fees        : ~$0.002 gas per tx on Polygon (negligible)
  - Capital at risk        : Never deploy more than you can afford to lose
  - Model risk             : Claude's accuracy is estimated at 65-70%
                             Real performance must be validated in paper mode
""")


def main():
    parser = argparse.ArgumentParser(description="Polymarket Strategy Backtester")
    parser.add_argument("--nocache", action="store_true", help="Fetch fresh data (ignore cache)")
    parser.add_argument("--quick", action="store_true", help="Skip sensitivity analysis")
    parser.add_argument("--capital", type=float, default=200.0, help="Starting capital in USD")
    parser.add_argument("--markets", type=int, default=1000, help="Number of markets to fetch")
    args = parser.parse_args()

    print("""
╔══════════════════════════════════════════════════════╗
║     POLYMARKET STRATEGY BACKTESTER                   ║
║     Using real resolved market data                  ║
╚══════════════════════════════════════════════════════╝
    """)

    # ── Phase 1: Fetch data ──────────────────────────────────────────
    print_header("PHASE 1 — Fetching Historical Data")
    markets = fetch_resolved_markets(
        limit=args.markets,
        use_cache=not args.nocache,
    )

    if len(markets) < 20:
        print("\n  ERROR: Not enough markets fetched. Check your internet connection.")
        print("  Minimum 20 markets required for meaningful backtest.")
        sys.exit(1)

    print(f"\n  Sample: {len(markets)} resolved markets loaded.")

    # ── Phase 2: Run strategies ──────────────────────────────────────
    print_header("PHASE 2 — Running Strategies")

    print("\n  Running S1: Pure Arbitrage...")
    s1 = backtest_arbitrage(markets)
    print(s1)

    print("\n  Running S2: Edge-based directional (mispricing)...")
    s2 = backtest_edge_strategy(markets, min_edge=0.08, min_liquidity=5000)
    print(s2)

    print("\n  Running S3: Kelly portfolio simulation...")
    s3 = backtest_kelly_simulation(
        markets,
        initial_capital=args.capital,
        model_accuracy=0.68,
        min_edge=0.08,
    )
    print(s3)

    # ── Phase 3: Sensitivity analysis ───────────────────────────────
    if not args.quick:
        print_header("PHASE 3 — Sensitivity Analysis")
        sensitivity = sensitivity_analysis(markets, initial_capital=args.capital)
        print_sensitivity_table(sensitivity)

    # ── Phase 4: Verdict ────────────────────────────────────────────
    print_header("PHASE 4 — Verdict & Recommendations")
    print_verdict(s1, s2, s3, markets)


if __name__ == "__main__":
    main()
