from datetime import datetime
from sqlalchemy import create_engine, Column, String, Float, Boolean, DateTime, Integer, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from config.settings import Config

Base = declarative_base()
engine = create_engine(Config.DATABASE_URL)
Session = sessionmaker(bind=engine)


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(String, nullable=False)
    market_question = Column(String)
    token_id = Column(String)
    side = Column(String)           # YES or NO
    outcome = Column(String)        # BUY or SELL
    price_entry = Column(Float)     # Price paid
    price_exit = Column(Float)      # Price sold / resolved
    size = Column(Float)            # Shares bought
    cost_usdc = Column(Float)       # USDC spent
    pnl_usdc = Column(Float)        # Profit/loss in USDC
    edge_estimated = Column(Float)  # Edge at entry (our prob - market price)
    confidence = Column(Float)      # LLM confidence 0-1
    strategy = Column(String)       # arbitrage / mispricing / news_catalyst
    status = Column(String)         # open / closed / resolved
    paper = Column(Boolean, default=True)
    order_id = Column(String)
    reasoning = Column(Text)        # LLM reasoning
    postmortem = Column(Text)       # Postmortem if loss
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(String, nullable=False)
    question = Column(String)
    yes_price = Column(Float)
    no_price = Column(Float)
    volume = Column(Float)
    liquidity = Column(Float)
    end_date = Column(DateTime)
    snapshot_at = Column(DateTime, default=datetime.utcnow)


class PerformanceLog(Base):
    __tablename__ = "performance"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(DateTime, default=datetime.utcnow)
    total_trades = Column(Integer)
    win_rate = Column(Float)
    total_pnl = Column(Float)
    capital = Column(Float)
    roi_pct = Column(Float)


def init_db():
    Base.metadata.create_all(engine)


def get_session():
    return Session()
