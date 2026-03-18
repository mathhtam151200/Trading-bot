"""
Probability Analyzer — uses Claude + live news to estimate the true
probability of a market outcome, then computes the edge vs market price.
"""
from __future__ import annotations

import asyncio
import feedparser
import aiohttp
from dataclasses import dataclass
from typing import Optional
import anthropic
from config.settings import Config
from core.scanner import Market
from utils.logger import get_logger

logger = get_logger("analyzer")


@dataclass
class AnalysisResult:
    market_id: str
    question: str
    market_yes_price: float
    estimated_yes_prob: float
    edge: float                # estimated_prob - market_price (positive = market underprices YES)
    confidence: float          # 0.0 to 1.0 — how confident is Claude
    reasoning: str
    news_headlines: list[str]
    recommended_side: str      # "YES" or "NO"
    recommended_price: float   # which token to buy

    @property
    def is_actionable(self) -> bool:
        return (
            abs(self.edge) >= Config.MIN_EDGE_THRESHOLD
            and self.confidence >= 0.65
        )


class NewsResearcher:
    """Scrapes RSS feeds to gather context about a market question."""

    def __init__(self):
        self.feeds = Config.RSS_FEEDS

    async def fetch_feed(self, session: aiohttp.ClientSession, url: str) -> list[str]:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                content = await resp.text()
                feed = feedparser.parse(content)
                return [entry.title for entry in feed.entries[:5]]
        except Exception:
            return []

    async def get_relevant_headlines(self, question: str, max_headlines: int = 15) -> list[str]:
        """Fetch headlines from all RSS feeds concurrently."""
        async with aiohttp.ClientSession() as session:
            tasks = [self.fetch_feed(session, url) for url in self.feeds]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        all_headlines = []
        for result in results:
            if isinstance(result, list):
                all_headlines.extend(result)

        # Filter headlines that might be relevant using simple keyword matching
        keywords = self._extract_keywords(question)
        relevant = [h for h in all_headlines if any(kw.lower() in h.lower() for kw in keywords)]

        # If no relevant found, return top general headlines
        if not relevant:
            relevant = all_headlines[:max_headlines]

        return relevant[:max_headlines]

    def _extract_keywords(self, question: str) -> list[str]:
        """Extract key entities from the market question."""
        stop_words = {
            "will", "the", "a", "an", "be", "is", "are", "was", "were",
            "by", "in", "on", "at", "to", "for", "of", "and", "or",
            "before", "after", "this", "that", "which", "who", "what",
            "when", "where", "how", "yes", "no", "than", "more", "less"
        }
        words = question.replace("?", "").replace(",", "").split()
        return [w for w in words if len(w) > 3 and w.lower() not in stop_words]


class ProbabilityAnalyzer:
    """
    Uses Claude to estimate true probability of a market outcome
    given the question + current news context.
    """

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        self.researcher = NewsResearcher()

    async def analyze(self, market: Market) -> Optional[AnalysisResult]:
        """Full analysis pipeline: news fetch → Claude estimation → edge calculation."""
        try:
            headlines = await self.researcher.get_relevant_headlines(market.question)
            result = await asyncio.to_thread(self._call_claude, market, headlines)
            return result
        except Exception as e:
            logger.error(f"Analysis failed for {market.question[:60]}: {e}")
            return None

    def _call_claude(self, market: Market, headlines: list[str]) -> Optional[AnalysisResult]:
        """Calls Claude to estimate probability. Runs in thread pool."""

        headlines_text = "\n".join(f"- {h}" for h in headlines) if headlines else "No recent headlines found."

        prompt = f"""You are an expert prediction market analyst. Your job is to estimate the TRUE probability of a market outcome based on available evidence, then identify if the market is mispriced.

## Market Question
{market.question}

## Current Market Prices
- YES price: ${market.yes_price:.3f} (implied probability: {market.yes_price:.1%})
- NO price: ${market.no_price:.3f} (implied probability: {market.no_price:.1%})
- Days to resolution: {market.days_to_resolution:.1f if market.days_to_resolution else 'unknown'}
- Market liquidity: ${market.liquidity:,.0f} USDC

## Recent News Headlines (potentially relevant)
{headlines_text}

## Your Task
1. Analyze the market question carefully
2. Consider the news headlines for any relevant signals
3. Estimate the TRUE probability of YES outcome (0.0 to 1.0)
4. Assess your confidence level (0.0 to 1.0)

## Response Format (JSON only, no other text)
{{
    "estimated_yes_probability": <float 0.0-1.0>,
    "confidence": <float 0.0-1.0>,
    "reasoning": "<2-3 sentences explaining your estimate>",
    "key_factors": ["<factor1>", "<factor2>", "<factor3>"]
}}

IMPORTANT RULES:
- If you have very little information, set confidence below 0.5
- Be calibrated: a 70% estimate means you'd be wrong 30% of the time
- Account for base rates and historical patterns
- Do NOT be overconfident — prediction markets are hard
- Return ONLY the JSON object, nothing else"""

        try:
            message = self.client.messages.create(
                model=Config.ANTHROPIC_MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}]
            )

            import json
            text = message.content[0].text.strip()
            # Clean potential markdown code blocks
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text.strip())

            estimated_prob = float(data["estimated_yes_probability"])
            confidence = float(data["confidence"])
            reasoning = data.get("reasoning", "")

            # Clamp values
            estimated_prob = max(0.01, min(0.99, estimated_prob))
            confidence = max(0.0, min(1.0, confidence))

            edge = estimated_prob - market.yes_price

            # Determine which side to take
            if edge > 0:
                # Market underprices YES → buy YES
                recommended_side = "YES"
                recommended_price = market.yes_price
            else:
                # Market overprices YES → buy NO
                recommended_side = "NO"
                recommended_price = market.no_price
                # Recalculate edge from NO perspective
                edge = (1 - estimated_prob) - market.no_price

            logger.info(
                f"Analysis: {market.question[:60]} | "
                f"est={estimated_prob:.1%} mkt={market.yes_price:.1%} "
                f"edge={edge:+.1%} conf={confidence:.0%}"
            )

            return AnalysisResult(
                market_id=market.id,
                question=market.question,
                market_yes_price=market.yes_price,
                estimated_yes_prob=estimated_prob,
                edge=edge,
                confidence=confidence,
                reasoning=reasoning,
                news_headlines=headlines,
                recommended_side=recommended_side,
                recommended_price=recommended_price,
            )

        except Exception as e:
            logger.error(f"Claude analysis error: {e}")
            return None

    async def analyze_batch(self, markets: list[Market], max_concurrent: int = 5) -> list[AnalysisResult]:
        """Analyze multiple markets concurrently with rate limiting."""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def analyze_with_limit(market: Market) -> Optional[AnalysisResult]:
            async with semaphore:
                return await self.analyze(market)

        results = await asyncio.gather(*[analyze_with_limit(m) for m in markets])
        valid = [r for r in results if r is not None and r.is_actionable]
        logger.info(f"Actionable signals: {len(valid)} / {len(markets)} analyzed")
        return valid
