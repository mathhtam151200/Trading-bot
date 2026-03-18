"""
Paper Trading Simulator — runs the full bot in paper mode.
No real money is used. All trades are logged to the database.

Use this for at least 1-2 weeks before going live.
Track: win rate, avg edge, calibration of Claude's estimates.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from config.settings import Config
from core.scanner import MarketScanner
from core.analyzer import ProbabilityAnalyzer
from core.risk_manager import RiskManager
from core.executor import TradeExecutor
from core.monitor import PositionMonitor
from core.postmortem import PostmortemAnalyzer
from utils.database import init_db, get_session, Trade, PerformanceLog
from utils.logger import get_logger
from utils.telegram import notifier

logger = get_logger("paper_trading")

# Force paper mode
Config.PAPER_TRADING = True


class PaperTradingSimulator:
    def __init__(self):
        self.scanner = MarketScanner()
        self.analyzer = ProbabilityAnalyzer()
        self.risk_manager = RiskManager()
        self.executor = TradeExecutor()
        self.monitor = PositionMonitor()
        self.postmortem = PostmortemAnalyzer()

        self.capital = Config.TOTAL_CAPITAL
        self.cycle_count = 0
        self.stats = {
            "signals_found": 0,
            "trades_placed": 0,
            "arb_trades": 0,
        }

    async def run_cycle(self):
        """One full scan-analyze-trade cycle."""
        self.cycle_count += 1
        logger.info(f"=== Cycle #{self.cycle_count} | Capital: ${self.capital:.2f} ===")

        async with self.scanner as scanner:
            scan_results = await scanner.scan()

        arb_markets = scan_results["arbitrage"]
        tradeable_markets = scan_results["tradeable"]

        # --- 1. Execute arbitrage first (risk-free) ---
        for market in arb_markets[:3]:  # max 3 arb trades per cycle
            arb_size = min(self.capital * 0.05, 20)  # 5% or $20 max per arb
            if arb_size < 2:
                continue
            result = self.executor.execute_arbitrage(
                market_id=market.id,
                yes_token_id=market.yes_token_id,
                no_token_id=market.no_token_id,
                yes_price=market.yes_price,
                no_price=market.no_price,
                size_usdc=arb_size
            )
            if result.get("success"):
                guaranteed_profit = arb_size * abs(market.spread)
                logger.info(f"ARB executed: +${guaranteed_profit:.3f} guaranteed | {market.question[:60]}")
                self.stats["arb_trades"] += 1
                self._save_trade(
                    market=market,
                    side="ARB",
                    price=market.yes_price + market.no_price,
                    size_shares=arb_size,
                    cost_usdc=arb_size,
                    strategy="arbitrage",
                    signal=None,
                    order_id=f"PAPER-ARB-{self.cycle_count}"
                )

        # --- 2. Analyze top tradeable markets for mispricing ---
        # Limit to top 20 by liquidity to save Claude API calls
        top_markets = tradeable_markets[:20]
        logger.info(f"Analyzing {len(top_markets)} markets for mispricing...")

        signals = await self.analyzer.analyze_batch(top_markets, max_concurrent=3)
        self.stats["signals_found"] += len(signals)

        # Check portfolio exposure before trading
        summary = await self.monitor.get_portfolio_summary()
        open_exposure = summary.get("open_exposure_usdc", 0)

        for signal in signals:
            if not self.risk_manager.validate_portfolio_risk(open_exposure):
                logger.warning("Portfolio exposure too high — skipping remaining signals")
                break

            sizing = self.risk_manager.compute_position(signal)
            if not sizing.allowed:
                logger.debug(f"Blocked: {sizing.reason}")
                continue

            result = self.executor.execute(signal, sizing)
            if result.success:
                self.stats["trades_placed"] += 1
                open_exposure += sizing.size_usdc

                # Save to DB
                trade_id = self._save_trade(
                    market=None,
                    side=signal.recommended_side,
                    price=sizing.entry_price,
                    size_shares=sizing.size_shares,
                    cost_usdc=sizing.size_usdc,
                    strategy="mispricing",
                    signal=signal,
                    order_id=result.order_id
                )

                notifier.signal_found(
                    market=signal.question,
                    side=signal.recommended_side,
                    edge=signal.edge,
                    confidence=signal.confidence,
                    size_usdc=sizing.size_usdc,
                    strategy="mispricing",
                    paper=True
                )

        # --- 3. Monitor existing positions ---
        closed = await self.monitor.check_open_positions()
        for item in closed:
            pnl = item["pnl"]
            self.capital += pnl
            self.risk_manager.update_capital(self.capital)

            if pnl < 0:
                # Run postmortem on losses
                self.postmortem.run_postmortem(item["trade"])

        self._log_performance()

    def _save_trade(self, market, side, price, size_shares, cost_usdc,
                    strategy, signal, order_id) -> int:
        session = get_session()
        try:
            trade = Trade(
                market_id=market.id if market else (signal.market_id if signal else ""),
                market_question=market.question if market else (signal.question if signal else ""),
                side=side,
                price_entry=price,
                size=size_shares,
                cost_usdc=cost_usdc,
                edge_estimated=signal.edge if signal else 0,
                confidence=signal.confidence if signal else 1.0,
                strategy=strategy,
                status="open",
                paper=True,
                order_id=order_id,
                reasoning=signal.reasoning if signal else "Arbitrage",
            )
            session.add(trade)
            session.commit()
            return trade.id
        finally:
            session.close()

    def _log_performance(self):
        """Save performance snapshot to DB."""
        session = get_session()
        try:
            from utils.database import Trade as TradeModel
            closed = session.query(TradeModel).filter(
                TradeModel.status.in_(["closed", "resolved"])
            ).all()

            if not closed:
                return

            wins = [t for t in closed if (t.pnl_usdc or 0) > 0]
            total_pnl = sum(t.pnl_usdc or 0 for t in closed)
            win_rate = len(wins) / len(closed) if closed else 0
            roi = total_pnl / Config.TOTAL_CAPITAL

            log = PerformanceLog(
                total_trades=len(closed),
                win_rate=win_rate,
                total_pnl=total_pnl,
                capital=self.capital,
                roi_pct=roi,
            )
            session.add(log)
            session.commit()

            logger.info(
                f"Performance: {len(closed)} trades | "
                f"WR={win_rate:.1%} | PnL=${total_pnl:+.2f} | ROI={roi:.1%}"
            )
        except Exception as e:
            logger.error(f"Performance log error: {e}")
        finally:
            session.close()

    async def run(self, cycles: int = None, interval_seconds: int = None):
        """
        Run paper trading loop.
        cycles=None → run forever
        interval_seconds → override config scan interval
        """
        interval = interval_seconds or Config.SCAN_INTERVAL
        cycle_num = 0

        logger.info(f"Starting paper trading | Capital: ${self.capital:.2f} | Interval: {interval}s")
        notifier.notify(f"🤖 Paper Trading Bot Started\n💰 Capital: ${self.capital:.2f}\n⏱ Scan every {interval}s")

        try:
            while True:
                try:
                    await self.run_cycle()
                except Exception as e:
                    logger.error(f"Cycle error: {e}", exc_info=True)

                cycle_num += 1
                if cycles and cycle_num >= cycles:
                    break

                logger.info(f"Sleeping {interval}s until next scan...")
                await asyncio.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Paper trading stopped by user")
            summary = await self.monitor.get_portfolio_summary()
            logger.info(f"Final summary: {summary}")
            notifier.daily_summary(
                trades=summary["total_trades"],
                win_rate=summary["win_rate"],
                pnl=summary["total_pnl"],
                capital=summary["current_capital"]
            )


async def main():
    init_db()
    sim = PaperTradingSimulator()
    await sim.run()


if __name__ == "__main__":
    asyncio.run(main())
