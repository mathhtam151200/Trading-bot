"""
Market Scanner — fetches active Polymarket markets via Gamma API
and filters for tradeable opportunities.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional
import aiohttp
from pydantic import BaseModel
from config.settings import Config
from utils.logger import get_logger

logger = get_logger("scanner")

GAMMA_BASE = Config.GAMMA_HOST


class Market(BaseModel):
    id: str
    question: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    volume: float
    liquidity: float
    end_date: Optional[datetime]
    active: bool
    closed: bool
    tags: list[str] = []

    @property
    def spread(self) -> float:
        """Spread between YES + NO and 1.0 — negative means arb opportunity."""
        return (self.yes_price + self.no_price) - 1.0

    @property
    def has_arbitrage(self) -> bool:
        """True when YES + NO < 1.0 (guaranteed profit at settlement)."""
        return self.spread < -0.005  # 0.5% buffer for fees

    @property
    def days_to_resolution(self) -> Optional[float]:
        if not self.end_date:
            return None
        delta = self.end_date - datetime.now(timezone.utc)
        return max(delta.total_seconds() / 86400, 0)

    def is_tradeable(self) -> bool:
        """Basic filter: active, enough liquidity, not expired."""
        if self.closed or not self.active:
            return False
        if self.liquidity < Config.MIN_MARKET_LIQUIDITY:
            return False
        if self.days_to_resolution is not None and self.days_to_resolution < 0.1:
            return False
        # Skip extreme prices (near certainty, low edge potential)
        if self.yes_price < 0.02 or self.yes_price > 0.98:
            return False
        return True


class MarketScanner:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def _get(self, endpoint: str, params: dict = None) -> list | dict:
        url = f"{GAMMA_BASE}{endpoint}"
        try:
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                return await resp.json()
        except Exception as e:
            logger.error(f"Gamma API error {endpoint}: {e}")
            return []

    def _parse_market(self, raw: dict) -> Optional[Market]:
        try:
            tokens = raw.get("tokens", [])
            if len(tokens) < 2:
                return None

            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), tokens[0])
            no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), tokens[1])

            yes_price = float(yes_token.get("price", 0))
            no_price = float(no_token.get("price", 0))

            if yes_price <= 0 or no_price <= 0:
                return None

            end_date = None
            if raw.get("endDate"):
                try:
                    end_date = datetime.fromisoformat(raw["endDate"].replace("Z", "+00:00"))
                except Exception:
                    pass

            return Market(
                id=raw.get("id", ""),
                question=raw.get("question", ""),
                condition_id=raw.get("conditionId", ""),
                yes_token_id=yes_token.get("token_id", ""),
                no_token_id=no_token.get("token_id", ""),
                yes_price=yes_price,
                no_price=no_price,
                volume=float(raw.get("volume24hr", raw.get("volume", 0))),
                liquidity=float(raw.get("liquidity", 0)),
                end_date=end_date,
                active=raw.get("active", True),
                closed=raw.get("closed", False),
                tags=[t.get("label", "") for t in raw.get("tags", [])],
            )
        except Exception as e:
            logger.debug(f"Failed to parse market: {e}")
            return None

    async def fetch_all_markets(self, limit: int = 500) -> list[Market]:
        """Fetch all active markets from Gamma API."""
        markets = []
        offset = 0
        batch = 100

        while offset < limit:
            params = {
                "active": "true",
                "closed": "false",
                "limit": batch,
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            }
            data = await self._get("/markets", params)
            if not data:
                break

            batch_markets = [m for raw in data if (m := self._parse_market(raw))]
            markets.extend(batch_markets)
            logger.debug(f"Fetched {len(batch_markets)} markets (offset {offset})")

            if len(data) < batch:
                break
            offset += batch

        logger.info(f"Total markets fetched: {len(markets)}")
        return markets

    async def get_tradeable_markets(self) -> list[Market]:
        """Returns filtered list of tradeable markets."""
        all_markets = await self.fetch_all_markets()
        tradeable = [m for m in all_markets if m.is_tradeable()]
        logger.info(f"Tradeable markets: {len(tradeable)} / {len(all_markets)}")
        return tradeable

    async def get_arbitrage_opportunities(self) -> list[Market]:
        """Returns markets where YES + NO < 1.0 (risk-free arb)."""
        all_markets = await self.fetch_all_markets()
        arb = [m for m in all_markets if m.has_arbitrage and m.active and not m.closed]
        if arb:
            logger.info(f"Arbitrage opportunities found: {len(arb)}")
            for m in arb:
                logger.info(f"  ARB: {m.question[:60]} | spread={m.spread:.4f}")
        return arb

    async def scan(self) -> dict[str, list[Market]]:
        """Full scan — returns arbitrage + tradeable markets."""
        tradeable = await self.get_tradeable_markets()
        arb = [m for m in tradeable if m.has_arbitrage]

        # Sort by liquidity (most liquid = easier to execute)
        tradeable.sort(key=lambda m: m.liquidity, reverse=True)

        return {
            "arbitrage": arb,
            "tradeable": tradeable,
        }
