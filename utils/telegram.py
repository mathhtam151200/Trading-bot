import asyncio
from telegram import Bot
from telegram.error import TelegramError
from config.settings import Config
from utils.logger import get_logger

logger = get_logger("telegram")


class TelegramNotifier:
    def __init__(self):
        self.enabled = bool(Config.TELEGRAM_BOT_TOKEN and Config.TELEGRAM_CHAT_ID)
        self.bot = Bot(token=Config.TELEGRAM_BOT_TOKEN) if self.enabled else None
        self.chat_id = Config.TELEGRAM_CHAT_ID

    async def send(self, message: str):
        if not self.enabled:
            logger.debug(f"[TELEGRAM DISABLED] {message}")
            return
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode="Markdown"
            )
        except TelegramError as e:
            logger.warning(f"Telegram error: {e}")

    def notify(self, message: str):
        """Sync wrapper for sending messages."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.send(message))
            else:
                loop.run_until_complete(self.send(message))
        except Exception as e:
            logger.warning(f"Could not send Telegram notification: {e}")

    def signal_found(self, market: str, side: str, edge: float, confidence: float,
                     size_usdc: float, strategy: str, paper: bool):
        mode = "[PAPER]" if paper else "[LIVE]"
        msg = (
            f"🎯 *Signal Found* {mode}\n"
            f"📊 Market: {market[:80]}\n"
            f"📈 Side: *{side}*\n"
            f"⚡ Strategy: {strategy}\n"
            f"📐 Edge: *{edge:.1%}*\n"
            f"🎲 Confidence: {confidence:.1%}\n"
            f"💰 Size: *${size_usdc:.2f} USDC*"
        )
        self.notify(msg)

    def trade_placed(self, market: str, side: str, price: float, size: float, paper: bool):
        mode = "[PAPER]" if paper else "[LIVE]"
        self.notify(
            f"✅ *Trade Placed* {mode}\n"
            f"Market: {market[:80]}\n"
            f"Side: {side} @ ${price:.3f}\n"
            f"Size: {size} shares | Cost: ${price * size:.2f}"
        )

    def trade_closed(self, market: str, pnl: float, paper: bool):
        mode = "[PAPER]" if paper else "[LIVE]"
        emoji = "🟢" if pnl >= 0 else "🔴"
        self.notify(
            f"{emoji} *Trade Closed* {mode}\n"
            f"Market: {market[:80]}\n"
            f"PnL: *${pnl:+.2f} USDC*"
        )

    def daily_summary(self, trades: int, win_rate: float, pnl: float, capital: float):
        self.notify(
            f"📊 *Daily Summary*\n"
            f"Trades: {trades}\n"
            f"Win Rate: {win_rate:.1%}\n"
            f"PnL: *${pnl:+.2f}*\n"
            f"Capital: ${capital:.2f}"
        )


notifier = TelegramNotifier()
