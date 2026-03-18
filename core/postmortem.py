"""
Postmortem Analyzer — after every losing trade, uses Claude to understand
what went wrong and updates the system's trading knowledge.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import anthropic
from config.settings import Config
from utils.database import get_session, Trade
from utils.logger import get_logger

logger = get_logger("postmortem")

KNOWLEDGE_FILE = Path("data/db/lessons_learned.json")


class PostmortemAnalyzer:
    """
    After a loss, Claude analyzes:
    1. What information was available at trade entry
    2. What actually happened
    3. Why the estimate was wrong
    4. What to watch for next time

    Lessons are saved to disk and injected into future analyses.
    """

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        self.lessons: list[dict] = self._load_lessons()

    def _load_lessons(self) -> list[dict]:
        if KNOWLEDGE_FILE.exists():
            try:
                return json.loads(KNOWLEDGE_FILE.read_text())
            except Exception:
                pass
        return []

    def _save_lessons(self):
        KNOWLEDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        KNOWLEDGE_FILE.write_text(json.dumps(self.lessons, indent=2, default=str))

    def run_postmortem(self, trade: Trade) -> str:
        """Run a postmortem analysis on a losing trade."""
        if (trade.pnl_usdc or 0) >= 0:
            return ""  # Only run on losses

        prompt = f"""You are a trading analyst running a postmortem on a losing prediction market trade.

## Trade Details
- Market: {trade.market_question}
- Side taken: {trade.side}
- Entry price: ${trade.price_entry:.3f} (our estimated prob: ~{trade.price_entry + trade.edge_estimated:.1%})
- Exit price: ${trade.price_exit:.3f}
- Loss: ${trade.pnl_usdc:.2f} USDC
- Strategy: {trade.strategy}
- Confidence at entry: {trade.confidence:.0%}

## Our Reasoning at Entry
{trade.reasoning}

## Previous Lessons Learned
{json.dumps(self.lessons[-5:], indent=2) if self.lessons else "None yet"}

## Your Task
Analyze why this trade lost. Consider:
1. Was our probability estimate overconfident?
2. Was there information we missed or misweighted?
3. Was this a case of correct process but bad luck (acceptable loss)?
4. What should we check next time before taking a similar trade?

## Response Format (JSON only)
{{
    "root_cause": "<main reason the trade lost>",
    "was_process_correct": <true/false>,
    "key_lesson": "<one concrete thing to do differently next time>",
    "flag_for_future": "<pattern to watch for in similar markets>",
    "severity": "<low|medium|high - how avoidable was this loss>"
}}"""

        try:
            message = self.client.messages.create(
                model=Config.ANTHROPIC_MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}]
            )
            text = message.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text.strip())

            lesson = {
                "date": datetime.utcnow().isoformat(),
                "market": trade.market_question,
                "loss_usdc": trade.pnl_usdc,
                **data
            }
            self.lessons.append(lesson)
            self._save_lessons()

            summary = (
                f"Postmortem: {data.get('root_cause', 'unknown')} | "
                f"Lesson: {data.get('key_lesson', '')} | "
                f"Severity: {data.get('severity', 'unknown')}"
            )
            logger.info(f"Postmortem saved: {summary}")

            # Update trade in DB
            session = get_session()
            try:
                session.query(Trade).filter(Trade.id == trade.id).update({
                    "postmortem": json.dumps(data)
                })
                session.commit()
            finally:
                session.close()

            return summary

        except Exception as e:
            logger.error(f"Postmortem error: {e}")
            return ""

    def run_batch_postmortems(self):
        """Run postmortems on all unanalyzed losing trades."""
        session = get_session()
        try:
            losses = session.query(Trade).filter(
                Trade.pnl_usdc < 0,
                Trade.postmortem.is_(None),
                Trade.status.in_(["closed", "resolved"])
            ).all()

            logger.info(f"Running postmortems on {len(losses)} losing trades")
            for trade in losses:
                self.run_postmortem(trade)
        finally:
            session.close()

    def get_lessons_summary(self) -> str:
        """Returns a formatted summary of recent lessons for injection into analysis."""
        if not self.lessons:
            return "No lessons learned yet."
        recent = self.lessons[-10:]
        lines = ["Recent lessons from losing trades:"]
        for lesson in recent:
            lines.append(f"- {lesson.get('key_lesson', '')} (severity: {lesson.get('severity', 'unknown')})")
        return "\n".join(lines)
