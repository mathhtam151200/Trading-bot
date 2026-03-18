"""
Trade Executor — places orders via Polymarket CLOB API.
Supports both live execution and paper trading mode.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from dataclasses import dataclass

from config.settings import Config
from core.analyzer import AnalysisResult
from core.risk_manager import PositionSizing
from utils.logger import get_logger
from utils.telegram import notifier

logger = get_logger("executor")


@dataclass
class TradeResult:
    success: bool
    order_id: Optional[str]
    market_id: str
    question: str
    side: str
    price: float
    size_shares: float
    cost_usdc: float
    paper: bool
    error: Optional[str] = None


class TradeExecutor:
    """
    Executes trades on Polymarket.
    In PAPER mode: simulates trades and logs them.
    In LIVE mode: uses py-clob-client to place real orders.
    """

    def __init__(self):
        self.paper = Config.PAPER_TRADING
        self._client = None

        if not self.paper:
            self._init_live_client()

    def _init_live_client(self):
        """Initialize the CLOB client for live trading."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            self._client = ClobClient(
                host=Config.CLOB_HOST,
                key=Config.PRIVATE_KEY,
                chain_id=137,  # Polygon
                signature_type=Config.SIGNATURE_TYPE,
                funder=Config.FUNDER_ADDRESS,
            )
            self._client.set_api_creds(self._client.create_or_derive_api_creds())
            logger.info("CLOB client initialized (LIVE mode)")
        except ImportError:
            logger.error("py-clob-client not installed. Run: pip install py-clob-client")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}")
            raise

    def execute(self, signal: AnalysisResult, sizing: PositionSizing) -> TradeResult:
        """Execute a trade based on signal and sizing."""
        if not sizing.allowed:
            return TradeResult(
                success=False,
                order_id=None,
                market_id=signal.market_id,
                question=signal.question,
                side=signal.recommended_side,
                price=sizing.entry_price,
                size_shares=0,
                cost_usdc=0,
                paper=self.paper,
                error=f"Blocked by risk manager: {sizing.reason}"
            )

        if self.paper:
            return self._paper_execute(signal, sizing)
        else:
            return self._live_execute(signal, sizing)

    def _paper_execute(self, signal: AnalysisResult, sizing: PositionSizing) -> TradeResult:
        """Simulate a trade without touching real money."""
        import uuid
        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"

        logger.info(
            f"[PAPER TRADE] {signal.recommended_side} {signal.question[:60]} | "
            f"${sizing.entry_price:.3f} x {sizing.size_shares:.2f} shares = ${sizing.size_usdc:.2f}"
        )

        notifier.trade_placed(
            market=signal.question,
            side=signal.recommended_side,
            price=sizing.entry_price,
            size=sizing.size_shares,
            paper=True
        )

        return TradeResult(
            success=True,
            order_id=order_id,
            market_id=signal.market_id,
            question=signal.question,
            side=signal.recommended_side,
            price=sizing.entry_price,
            size_shares=sizing.size_shares,
            cost_usdc=sizing.size_usdc,
            paper=True,
        )

    def _live_execute(self, signal: AnalysisResult, sizing: PositionSizing) -> TradeResult:
        """Place a real limit order on Polymarket."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            # Determine token ID based on side
            # We need the market's token_id — passed via signal's market reference
            # For now we use the token_id stored in the signal
            token_id = signal.market_id  # overridden in main with correct token_id

            order_args = OrderArgs(
                token_id=token_id,
                price=round(sizing.entry_price, 4),
                size=round(sizing.size_shares, 2),
                side="BUY",
            )

            signed_order = self._client.create_order(order_args)
            resp = self._client.post_order(signed_order, OrderType.GTC)

            order_id = resp.get("orderID", "") if resp else ""
            success = bool(order_id)

            if success:
                logger.info(
                    f"[LIVE TRADE] Order placed: {order_id} | "
                    f"{signal.recommended_side} @ ${sizing.entry_price:.3f} | "
                    f"${sizing.size_usdc:.2f}"
                )
                notifier.trade_placed(
                    market=signal.question,
                    side=signal.recommended_side,
                    price=sizing.entry_price,
                    size=sizing.size_shares,
                    paper=False
                )
            else:
                logger.error(f"Order placement failed: {resp}")

            return TradeResult(
                success=success,
                order_id=order_id,
                market_id=signal.market_id,
                question=signal.question,
                side=signal.recommended_side,
                price=sizing.entry_price,
                size_shares=sizing.size_shares,
                cost_usdc=sizing.size_usdc,
                paper=False,
                error=None if success else f"API returned: {resp}"
            )

        except Exception as e:
            logger.error(f"Live execution error: {e}")
            return TradeResult(
                success=False,
                order_id=None,
                market_id=signal.market_id,
                question=signal.question,
                side=signal.recommended_side,
                price=sizing.entry_price,
                size_shares=sizing.size_shares,
                cost_usdc=sizing.size_usdc,
                paper=False,
                error=str(e)
            )

    def execute_arbitrage(self, market_id: str, yes_token_id: str, no_token_id: str,
                          yes_price: float, no_price: float, size_usdc: float) -> dict:
        """
        Execute risk-free arbitrage: buy both YES and NO when YES + NO < 1.0.
        Guaranteed $1.00 at settlement for each pair of shares bought.
        """
        cost = yes_price + no_price  # e.g. 0.92 total
        guaranteed_profit_pct = (1.0 - cost) / cost

        if self.paper:
            logger.info(
                f"[PAPER ARB] Buy YES @ {yes_price:.3f} + NO @ {no_price:.3f} = {cost:.3f} | "
                f"Guaranteed profit: {guaranteed_profit_pct:.1%} | ${size_usdc:.2f}"
            )
            return {"success": True, "paper": True, "profit_pct": guaranteed_profit_pct}

        # Live arb: place both orders
        results = {}
        for side, token_id, price in [("YES", yes_token_id, yes_price), ("NO", no_token_id, no_price)]:
            try:
                from py_clob_client.clob_types import OrderArgs, OrderType
                shares = (size_usdc / 2) / price
                order_args = OrderArgs(token_id=token_id, price=round(price, 4), size=round(shares, 2), side="BUY")
                signed = self._client.create_order(order_args)
                resp = self._client.post_order(signed, OrderType.FOK)  # Fill or Kill for arb
                results[side] = resp
            except Exception as e:
                logger.error(f"Arb leg {side} failed: {e}")
                results[side] = {"error": str(e)}

        return results
