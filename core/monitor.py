"""
Position Monitor — tracks open positions and closes them
when target price is reached or market resolves.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
import aiohttp
from config.settings import Config
from utils.logger import get_logger
from utils.telegram import notifier
from utils.database import get_session, Trade

logger = get_logger("monitor")


class PositionMonitor:
    """
    Monitors open trades and:
    1. Detects when markets resolve (closed = True)
    2. Tracks current PnL for each open position
    3. Sends exit signals when target profit is reached
    4. Updates the database with final PnL
    """

    def __init__(self):
        self.gamma_host = Config.GAMMA_HOST

    async def get_current_price(self, market_id: str) -> Optional[dict]:
        """Fetch current YES/NO prices for a market."""
        url = f"{self.gamma_host}/markets/{market_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception as e:
            logger.debug(f"Price fetch error for {market_id}: {e}")
        return None

    async def check_open_positions(self) -> list[dict]:
        """
        Scan all open trades in the database and update their status.
        Returns list of trades that were closed/resolved.
        """
        session = get_session()
        closed_trades = []

        try:
            open_trades = session.query(Trade).filter(Trade.status == "open").all()
            logger.debug(f"Monitoring {len(open_trades)} open positions")

            for trade in open_trades:
                update = await self._check_trade(trade)
                if update:
                    trade.status = update["status"]
                    trade.price_exit = update["exit_price"]
                    trade.pnl_usdc = update["pnl"]
                    trade.closed_at = datetime.utcnow()
                    session.commit()
                    closed_trades.append({
                        "trade": trade,
                        "pnl": update["pnl"]
                    })

                    notifier.trade_closed(
                        market=trade.market_question or "",
                        pnl=update["pnl"],
                        paper=trade.paper
                    )
        except Exception as e:
            logger.error(f"Monitor error: {e}")
        finally:
            session.close()

        return closed_trades

    async def _check_trade(self, trade: Trade) -> Optional[dict]:
        """Check a single trade's current status."""
        market_data = await self.get_current_price(trade.market_id)
        if not market_data:
            return None

        # Check if market resolved
        if market_data.get("closed") or market_data.get("resolved"):
            tokens = market_data.get("tokens", [])
            winning_outcome = None

            for token in tokens:
                if token.get("winner"):
                    winning_outcome = token.get("outcome", "").upper()
                    break

            if winning_outcome:
                won = (trade.side == winning_outcome)
                if won:
                    # Full win: shares resolve to $1.00 each
                    exit_price = 1.0
                    pnl = (trade.size * 1.0) - trade.cost_usdc
                else:
                    # Full loss: shares resolve to $0.00
                    exit_price = 0.0
                    pnl = -trade.cost_usdc

                logger.info(
                    f"Market resolved: {trade.market_question[:50]} | "
                    f"Won: {won} | PnL: ${pnl:+.2f}"
                )
                return {"status": "resolved", "exit_price": exit_price, "pnl": round(pnl, 2)}

        # Check for early exit opportunity (target profit)
        tokens = market_data.get("tokens", [])
        current_price = None
        for token in tokens:
            if token.get("outcome", "").upper() == trade.side:
                current_price = float(token.get("price", 0))
                break

        if current_price and trade.price_entry:
            unrealized_pnl = (current_price - trade.price_entry) * (trade.size or 0)

            # Take profit at 50%+ gain or if market moved strongly in our favor
            profit_pct = (current_price - trade.price_entry) / trade.price_entry
            if profit_pct >= 0.50:
                logger.info(
                    f"Take profit triggered: {trade.market_question[:50]} | "
                    f"{profit_pct:.1%} gain | PnL: ${unrealized_pnl:+.2f}"
                )
                pnl = round(unrealized_pnl, 2)
                return {"status": "closed", "exit_price": current_price, "pnl": pnl}

        return None

    async def get_portfolio_summary(self) -> dict:
        """Returns current portfolio stats from the database."""
        session = get_session()
        try:
            all_trades = session.query(Trade).all()
            closed = [t for t in all_trades if t.status in ("closed", "resolved")]
            open_trades = [t for t in all_trades if t.status == "open"]

            total_pnl = sum(t.pnl_usdc or 0 for t in closed)
            wins = [t for t in closed if (t.pnl_usdc or 0) > 0]
            win_rate = len(wins) / len(closed) if closed else 0

            open_exposure = sum(t.cost_usdc or 0 for t in open_trades)

            return {
                "total_trades": len(all_trades),
                "closed_trades": len(closed),
                "open_trades": len(open_trades),
                "win_rate": win_rate,
                "total_pnl": round(total_pnl, 2),
                "open_exposure_usdc": round(open_exposure, 2),
                "current_capital": round(Config.TOTAL_CAPITAL + total_pnl, 2),
            }
        finally:
            session.close()
