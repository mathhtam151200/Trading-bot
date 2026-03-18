"""
Risk Manager — calculates optimal position size using Kelly Criterion
and enforces hard safety limits to protect capital.
"""
from __future__ import annotations

from dataclasses import dataclass
from config.settings import Config
from core.analyzer import AnalysisResult
from utils.logger import get_logger

logger = get_logger("risk_manager")


@dataclass
class PositionSizing:
    allowed: bool
    size_usdc: float
    size_shares: float
    entry_price: float
    max_loss_usdc: float
    expected_value: float
    kelly_pct: float
    reason: str


class RiskManager:
    """
    Applies Kelly Criterion with configurable fraction + hard caps.

    Kelly formula for binary outcomes:
        K = (p * b - (1 - p)) / b
    Where:
        p = probability of winning
        b = net odds (profit per unit risked = (1 - price) / price)
    """

    def __init__(self, capital: float = None):
        self.capital = capital or Config.TOTAL_CAPITAL
        self.max_position_pct = Config.MAX_POSITION_PCT
        self.kelly_fraction = Config.KELLY_FRACTION  # quarter kelly by default

    def update_capital(self, new_capital: float):
        """Called after each trade to update available capital."""
        self.capital = new_capital

    def kelly_fraction_for_trade(self, prob_win: float, entry_price: float) -> float:
        """
        Calculate Kelly fraction for a binary prediction market trade.

        Args:
            prob_win: Our estimated probability of winning (0-1)
            entry_price: Price paid per share (0-1)

        Returns:
            Kelly fraction of bankroll to risk (0-1)
        """
        if entry_price <= 0 or entry_price >= 1:
            return 0.0

        # Net odds: if we pay `price` and win $1, net profit = (1 - price)
        # Odds expressed as profit per unit risked
        b = (1.0 - entry_price) / entry_price  # e.g. price=0.30 → b=2.33
        p = prob_win
        q = 1 - p

        kelly = (p * b - q) / b

        # Apply fractional Kelly (safer)
        fractional = kelly * self.kelly_fraction

        return max(0.0, fractional)

    def compute_position(self, signal: AnalysisResult) -> PositionSizing:
        """
        Compute position size for a given signal.
        Returns PositionSizing with allowed=False if trade should be blocked.
        """
        entry_price = signal.recommended_price
        side = signal.recommended_side

        # Derive win probability for the chosen side
        if side == "YES":
            prob_win = signal.estimated_yes_prob
        else:
            prob_win = 1.0 - signal.estimated_yes_prob

        # --- Safety checks ---
        if prob_win < 0.5:
            return PositionSizing(
                allowed=False, size_usdc=0, size_shares=0,
                entry_price=entry_price, max_loss_usdc=0, expected_value=0,
                kelly_pct=0, reason="Probability below 50% — no edge"
            )

        if abs(signal.edge) < Config.MIN_EDGE_THRESHOLD:
            return PositionSizing(
                allowed=False, size_usdc=0, size_shares=0,
                entry_price=entry_price, max_loss_usdc=0, expected_value=0,
                kelly_pct=0, reason=f"Edge {signal.edge:.1%} below minimum {Config.MIN_EDGE_THRESHOLD:.1%}"
            )

        if signal.confidence < 0.65:
            return PositionSizing(
                allowed=False, size_usdc=0, size_shares=0,
                entry_price=entry_price, max_loss_usdc=0, expected_value=0,
                kelly_pct=0, reason=f"Confidence {signal.confidence:.1%} too low (min 65%)"
            )

        # --- Kelly calculation ---
        kelly_pct = self.kelly_fraction_for_trade(prob_win, entry_price)

        if kelly_pct <= 0:
            return PositionSizing(
                allowed=False, size_usdc=0, size_shares=0,
                entry_price=entry_price, max_loss_usdc=0, expected_value=0,
                kelly_pct=0, reason="Kelly criterion gives zero or negative bet"
            )

        # Apply hard cap
        capped_pct = min(kelly_pct, self.max_position_pct)

        size_usdc = self.capital * capped_pct

        # Minimum trade size ($2 to cover gas/fees)
        if size_usdc < 2.0:
            return PositionSizing(
                allowed=False, size_usdc=0, size_shares=0,
                entry_price=entry_price, max_loss_usdc=0, expected_value=0,
                kelly_pct=kelly_pct,
                reason=f"Position too small (${size_usdc:.2f} < $2.00 minimum)"
            )

        size_shares = size_usdc / entry_price
        max_loss = size_usdc  # worst case: price goes to 0
        expected_pnl = (prob_win * (size_shares - size_usdc)) - ((1 - prob_win) * size_usdc)

        if capped_pct < kelly_pct:
            cap_note = f" (capped from Kelly {kelly_pct:.1%})"
        else:
            cap_note = ""

        logger.info(
            f"Position sizing: {side} @ ${entry_price:.3f} | "
            f"${size_usdc:.2f} USDC ({capped_pct:.1%} of capital){cap_note} | "
            f"EV=${expected_pnl:.2f}"
        )

        return PositionSizing(
            allowed=True,
            size_usdc=round(size_usdc, 2),
            size_shares=round(size_shares, 4),
            entry_price=entry_price,
            max_loss_usdc=round(max_loss, 2),
            expected_value=round(expected_pnl, 2),
            kelly_pct=kelly_pct,
            reason=f"Kelly={kelly_pct:.1%} → Fractional={capped_pct:.1%}{cap_note}",
        )

    def validate_portfolio_risk(self, open_positions_usdc: float) -> bool:
        """
        Block new trades if total open exposure > 50% of capital.
        Prevents overtrading when multiple signals fire simultaneously.
        """
        exposure_pct = open_positions_usdc / self.capital if self.capital > 0 else 1.0
        if exposure_pct > 0.50:
            logger.warning(f"Portfolio exposure {exposure_pct:.1%} > 50% — blocking new trades")
            return False
        return True
