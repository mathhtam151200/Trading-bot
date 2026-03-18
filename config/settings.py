import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Polymarket
    PRIVATE_KEY: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    FUNDER_ADDRESS: str = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
    SIGNATURE_TYPE: int = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
    CLOB_HOST: str = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    GAMMA_HOST: str = os.getenv("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com")

    # Claude AI
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Trading parameters
    TOTAL_CAPITAL: float = float(os.getenv("TOTAL_CAPITAL", "200"))
    MAX_POSITION_PCT: float = float(os.getenv("MAX_POSITION_PCT", "0.10"))
    KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.25"))
    MIN_EDGE_THRESHOLD: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.08"))
    MIN_MARKET_LIQUIDITY: float = float(os.getenv("MIN_MARKET_LIQUIDITY", "5000"))
    PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///data/db/trades.db")

    # Scan intervals (seconds)
    SCAN_INTERVAL: int = 30
    MONITOR_INTERVAL: int = 60

    # News sources for research agent
    RSS_FEEDS: list = [
        "https://feeds.reuters.com/reuters/topNews",
        "https://rss.cnn.com/rss/edition.rss",
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://www.theguardian.com/world/rss",
        "https://feeds.npr.org/1001/rss.xml",
    ]

    @classmethod
    def validate(cls) -> list[str]:
        """Returns list of missing critical configs."""
        missing = []
        if not cls.PAPER_TRADING:
            if not cls.PRIVATE_KEY:
                missing.append("POLYMARKET_PRIVATE_KEY")
            if not cls.FUNDER_ADDRESS:
                missing.append("POLYMARKET_FUNDER_ADDRESS")
        if not cls.ANTHROPIC_API_KEY:
            missing.append("ANTHROPIC_API_KEY")
        return missing
