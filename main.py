"""
Polymarket Trading Bot — Main Entry Point

Phases:
  Phase 1 (paper):  PAPER_TRADING=true — learn the market, calibrate estimates
  Phase 2 (live):   PAPER_TRADING=false — small live trades after validation

Usage:
  python main.py                    # Run bot (paper or live based on .env)
  python main.py --paper            # Force paper mode
  python main.py --report           # Print performance report
  python main.py --postmortem       # Run postmortems on unanalyzed losses
  python main.py --scan             # Single scan and exit
"""
from __future__ import annotations

import asyncio
import argparse
import sys
from datetime import datetime

from config.settings import Config
from utils.database import init_db
from utils.logger import get_logger
from utils.telegram import notifier

logger = get_logger("main")


def print_banner():
    mode = "PAPER" if Config.PAPER_TRADING else "LIVE"
    capital = Config.TOTAL_CAPITAL
    print(f"""
╔══════════════════════════════════════════════════════╗
║         POLYMARKET TRADING BOT                       ║
║  Mode: {mode:<8}  Capital: ${capital:<8.2f}              ║
║  Min Edge: {Config.MIN_EDGE_THRESHOLD:.0%}   Kelly: {Config.KELLY_FRACTION:.0%} fraction        ║
║  Max Position: {Config.MAX_POSITION_PCT:.0%} of capital              ║
╚══════════════════════════════════════════════════════╝
    """)


async def run_bot(force_paper: bool = False):
    """Main bot loop."""
    if force_paper:
        Config.PAPER_TRADING = True

    # Validate config
    missing = Config.validate()
    if missing:
        logger.error(f"Missing configuration: {', '.join(missing)}")
        logger.error("Copy .env.example to .env and fill in your values")
        sys.exit(1)

    print_banner()
    init_db()

    from core.scanner import MarketScanner
    from core.analyzer import ProbabilityAnalyzer
    from core.risk_manager import RiskManager
    from core.executor import TradeExecutor
    from core.monitor import PositionMonitor
    from core.postmortem import PostmortemAnalyzer

    scanner = MarketScanner()
    analyzer = ProbabilityAnalyzer()
    risk_manager = RiskManager()
    executor = TradeExecutor()
    monitor = PositionMonitor()
    postmortem_analyzer = PostmortemAnalyzer()

    capital = Config.TOTAL_CAPITAL
    cycle = 0

    mode_str = "[PAPER]" if Config.PAPER_TRADING else "[LIVE]"
    logger.info(f"{mode_str} Bot started | ${capital:.2f} USDC | scanning every {Config.SCAN_INTERVAL}s")
    notifier.notify(f"🤖 Bot started {mode_str}\n💰 Capital: ${capital:.2f}")

    try:
        while True:
            cycle += 1
            logger.info(f"{'='*50}")
            logger.info(f"Cycle #{cycle} | {datetime.now().strftime('%H:%M:%S')} | Capital: ${capital:.2f}")

            try:
                # Step 1: Scan markets
                async with scanner as s:
                    scan_results = await s.scan()

                arb_markets = scan_results["arbitrage"]
                tradeable = scan_results["tradeable"]

                # Step 2: Arbitrage opportunities (risk-free, execute immediately)
                for market in arb_markets[:5]:
                    arb_usdc = min(capital * 0.05, 25)
                    if arb_usdc < 2:
                        continue
                    result = executor.execute_arbitrage(
                        market_id=market.id,
                        yes_token_id=market.yes_token_id,
                        no_token_id=market.no_token_id,
                        yes_price=market.yes_price,
                        no_price=market.no_price,
                        size_usdc=arb_usdc
                    )
                    logger.info(f"ARB: {market.question[:60]} | spread={market.spread:.4f}")

                # Step 3: Analyze top markets for mispricing (limit to save API calls)
                top_markets = tradeable[:20]
                if top_markets:
                    logger.info(f"Analyzing {len(top_markets)} markets...")
                    signals = await analyzer.analyze_batch(top_markets, max_concurrent=3)

                    # Step 4: Check portfolio exposure
                    summary = await monitor.get_portfolio_summary()
                    open_exposure = summary.get("open_exposure_usdc", 0)

                    for signal in signals:
                        if not risk_manager.validate_portfolio_risk(open_exposure):
                            break

                        sizing = risk_manager.compute_position(signal)
                        if not sizing.allowed:
                            continue

                        result = executor.execute(signal, sizing)
                        if result.success:
                            open_exposure += sizing.size_usdc
                            _save_trade_to_db(signal, sizing, result)

                            notifier.signal_found(
                                market=signal.question,
                                side=signal.recommended_side,
                                edge=signal.edge,
                                confidence=signal.confidence,
                                size_usdc=sizing.size_usdc,
                                strategy="mispricing",
                                paper=Config.PAPER_TRADING
                            )

                # Step 5: Monitor open positions
                closed_trades = await monitor.check_open_positions()
                for item in closed_trades:
                    pnl = item["pnl"]
                    capital += pnl
                    risk_manager.update_capital(capital)

                    if pnl < 0:
                        postmortem_analyzer.run_postmortem(item["trade"])

                # Step 6: Daily summary every 48 cycles (~24h at 30s intervals)
                if cycle % 48 == 0:
                    summary = await monitor.get_portfolio_summary()
                    notifier.daily_summary(
                        trades=summary["total_trades"],
                        win_rate=summary["win_rate"],
                        pnl=summary["total_pnl"],
                        capital=summary["current_capital"]
                    )

            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)

            await asyncio.sleep(Config.SCAN_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Bot stopped")
        summary = await monitor.get_portfolio_summary()
        logger.info(f"Final: {summary}")


def _save_trade_to_db(signal, sizing, result):
    """Save a trade to the database."""
    from utils.database import get_session, Trade
    session = get_session()
    try:
        trade = Trade(
            market_id=signal.market_id,
            market_question=signal.question,
            side=signal.recommended_side,
            price_entry=sizing.entry_price,
            size=sizing.size_shares,
            cost_usdc=sizing.size_usdc,
            edge_estimated=signal.edge,
            confidence=signal.confidence,
            strategy="mispricing",
            status="open",
            paper=result.paper,
            order_id=result.order_id,
            reasoning=signal.reasoning,
        )
        session.add(trade)
        session.commit()
    finally:
        session.close()


async def run_report():
    """Print a performance report."""
    init_db()
    from core.monitor import PositionMonitor
    monitor = PositionMonitor()
    summary = await monitor.get_portfolio_summary()

    print("\n" + "="*50)
    print("PERFORMANCE REPORT")
    print("="*50)
    for k, v in summary.items():
        if isinstance(v, float):
            if "rate" in k:
                print(f"  {k}: {v:.1%}")
            else:
                print(f"  {k}: ${v:.2f}")
        else:
            print(f"  {k}: {v}")

    from utils.database import get_session, Trade
    session = get_session()
    try:
        recent = session.query(Trade).order_by(Trade.opened_at.desc()).limit(10).all()
        if recent:
            print("\nRecent Trades:")
            print("-"*50)
            for t in recent:
                pnl_str = f"${t.pnl_usdc:+.2f}" if t.pnl_usdc is not None else "open"
                print(f"  [{t.status}] {t.side} | {t.market_question[:45]} | {pnl_str}")
    finally:
        session.close()


async def run_scan():
    """Single scan to test market detection."""
    from core.scanner import MarketScanner
    async with MarketScanner() as scanner:
        results = await scanner.scan()

    arb = results["arbitrage"]
    tradeable = results["tradeable"]

    print(f"\nArbitrage opportunities: {len(arb)}")
    for m in arb[:5]:
        print(f"  {m.question[:70]} | spread={m.spread:.4f} | liq=${m.liquidity:,.0f}")

    print(f"\nTop tradeable markets: {len(tradeable)}")
    for m in tradeable[:10]:
        print(f"  YES={m.yes_price:.3f} NO={m.no_price:.3f} | liq=${m.liquidity:,.0f} | {m.question[:60]}")


def main():
    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    parser.add_argument("--paper", action="store_true", help="Force paper trading mode")
    parser.add_argument("--report", action="store_true", help="Print performance report")
    parser.add_argument("--postmortem", action="store_true", help="Run postmortems on losses")
    parser.add_argument("--scan", action="store_true", help="Single scan and exit")
    args = parser.parse_args()

    if args.report:
        asyncio.run(run_report())
    elif args.postmortem:
        init_db()
        from core.postmortem import PostmortemAnalyzer
        PostmortemAnalyzer().run_batch_postmortems()
    elif args.scan:
        asyncio.run(run_scan())
    else:
        asyncio.run(run_bot(force_paper=args.paper))


if __name__ == "__main__":
    main()
