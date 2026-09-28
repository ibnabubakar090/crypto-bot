#!/usr/bin/env python3
"""
Solana Narrative Tracking Bot
Uses DexScreener for Solana market data and Telegram for alerts.
"""

import os
import time
import logging
import asyncio
from datetime import datetime
from typing import Optional, Dict, List, Tuple

import httpx
from telegram import Bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DEXSCREENER_API_BASE = "https://api.dexscreener.com/latest/dex"

ALERT_THRESHOLD = 65
MIN_COOLDOWN_HOURS = 4
CHECK_INTERVAL_MINUTES = 10
MIN_LIQUIDITY_USD = 50_000


class BotState:
    def __init__(self):
        self.seen_tokens = {}
        self.alert_history = {}

    def record_token(self, address: str, data: Dict):
        if address not in self.seen_tokens:
            self.seen_tokens[address] = {
                **data,
                "first_seen": time.time(),
            }
        else:
            self.seen_tokens[address].update(data)

    def can_alert(self, address: str) -> bool:
        if address not in self.alert_history:
            return True

        elapsed = time.time() - self.alert_history[address]
        return elapsed > MIN_COOLDOWN_HOURS * 3600

    def record_alert(self, address: str):
        self.alert_history[address] = time.time()

    def age_hours(self, address: str) -> float:
        if address not in self.seen_tokens:
            return 0

        return (time.time() - self.seen_tokens[address]["first_seen"]) / 3600


state = BotState()


async def fetch_solana_pairs() -> List[Dict]:
    """
    Search DexScreener for active Solana pairs.

    DexScreener search returns market pairs rather than a simple
    list of newly created tokens, so we filter and score the pairs.
    """

    try:
        async with httpx.AsyncClient(timeout=20) as client:

            # Search several broad Solana terms to discover active pairs.
            queries = [
                "SOL",
                "USDC",
                "USDT",
                "WIF",
                "BONK",
            ]

            all_pairs = {}

            for query in queries:
                response = await client.get(
                    f"{DEXSCREENER_API_BASE}/search",
                    params={"q": query},
                )

                response.raise_for_status()

                data = response.json()

                for pair in data.get("pairs", []):
                    if pair.get("chainId") != "solana":
                        continue

                    address = pair.get("pairAddress")

                    if address:
                        all_pairs[address] = pair

            pairs = list(all_pairs.values())

            logger.info(
                f"📊 DexScreener: Found {len(pairs)} Solana pairs"
            )

            return pairs

    except Exception as e:
        logger.error(f"DexScreener fetch error: {e}")
        return []


def get_pair_data(pair: Dict) -> Optional[Dict]:
    try:
        base_token = pair.get("baseToken", {})

        symbol = base_token.get("symbol", "UNKNOWN")
        name = base_token.get("name", "Unknown")
        address = base_token.get("address", "")

        liquidity = float(
            pair.get("liquidity", {}).get("usd", 0) or 0
        )

        volume_24h = float(
            pair.get("volume", {}).get("h24", 0) or 0
        )

        price_change_24h = float(
            pair.get("priceChange", {}).get("h24", 0) or 0
        )

        market_cap = float(
            pair.get("marketCap", 0) or 0
        )

        fdv = float(
            pair.get("fdv", 0) or 0
        )

        price = float(
            pair.get("priceUsd", 0) or 0
        )

        pair_created = pair.get("pairCreatedAt")

        return {
            "symbol": symbol,
            "name": name,
            "address": address,
            "pair_address": pair.get("pairAddress", ""),
            "price": price,
            "liquidity": liquidity,
            "volume_24h": volume_24h,
            "price_change_24h": price_change_24h,
            "market_cap": market_cap,
            "fdv": fdv,
            "pair_created_at": pair_created,
            "dex": pair.get("dexId", "unknown"),
            "url": pair.get(
                "url",
                f"https://dexscreener.com/solana/{pair.get('pairAddress', '')}"
            ),
        }

    except Exception as e:
        logger.error(f"Pair parsing error: {e}")
        return None


def score_token(data: Dict) -> Tuple[float, str]:

    score = 0
    reasons = []

    address = data["address"]

    # ---------------------------------------------------------
    # AGE
    # ---------------------------------------------------------

    age = state.age_hours(address)

    if age < 1:
        score += 15
        reasons.append("🆕 First seen < 1 hour")

    elif age < 6:
        score += 12
        reasons.append("🆕 First seen < 6 hours")

    elif age < 24:
        score += 8
        reasons.append("📅 First seen < 24 hours")

    elif age < 72:
        score += 3
        reasons.append("📅 First seen < 72 hours")

    # ---------------------------------------------------------
    # LIQUIDITY
    # ---------------------------------------------------------

    liquidity = data["liquidity"]

    if liquidity >= 1_000_000:
        score += 15
        reasons.append(
            f"💧 Strong liquidity (${liquidity:,.0f})"
        )

    elif liquidity >= 500_000:
        score += 12
        reasons.append(
            f"💧 Good liquidity (${liquidity:,.0f})"
        )

    elif liquidity >= 100_000:
        score += 8
        reasons.append(
            f"💧 Healthy liquidity (${liquidity:,.0f})"
        )

    elif liquidity >= MIN_LIQUIDITY_USD:
        score += 3
        reasons.append(
            f"💧 Minimum liquidity (${liquidity:,.0f})"
        )

    # ---------------------------------------------------------
    # VOLUME / LIQUIDITY
    # ---------------------------------------------------------

    volume = data["volume_24h"]

    if liquidity > 0:
        ratio = volume / liquidity
    else:
        ratio = 0

    if ratio >= 5:
        score += 20
        reasons.append(
            f"🔥 Extreme volume/liquidity ({ratio:.1f}x)"
        )

    elif ratio >= 2:
        score += 15
        reasons.append(
            f"🔥 High volume/liquidity ({ratio:.1f}x)"
        )

    elif ratio >= 1:
        score += 10
        reasons.append(
            f"📈 Strong trading activity ({ratio:.1f}x)"
        )

    elif ratio >= 0.5:
        score += 5
        reasons.append(
            f"📊 Moderate activity ({ratio:.1f}x)"
        )

    # ---------------------------------------------------------
    # MOMENTUM
    # ---------------------------------------------------------

    change = data["price_change_24h"]

    if change >= 100:
        score += 20
        reasons.append(
            f"🚀 Massive momentum (+{change:.0f}%)"
        )

    elif change >= 50:
        score += 15
        reasons.append(
            f"🚀 Strong momentum (+{change:.0f}%)"
        )

    elif change >= 20:
        score += 10
        reasons.append(
            f"📈 Positive momentum (+{change:.0f}%)"
        )

    elif change > 0:
        score += 3
        reasons.append(
            f"📊 Slight positive momentum (+{change:.0f}%)"
        )

    elif change <= -20:
        reasons.append(
            f"⛔ Heavy decline ({change:.0f}%)"
        )

    # ---------------------------------------------------------
    # MARKET CAP
    # ---------------------------------------------------------

    market_cap = data["market_cap"]

    if 0 < market_cap < 100_000_000:
        score += 15
        reasons.append(
            f"💎 Small market cap (${market_cap:,.0f})"
        )

    elif market_cap < 500_000_000:
        score += 10
        reasons.append(
            f"💎 Mid-small market cap (${market_cap:,.0f})"
        )

    elif market_cap < 1_000_000_000:
        score += 5
        reasons.append(
            f"📊 Mid-cap (${market_cap:,.0f})"
        )

    # ---------------------------------------------------------
    # FINAL SCORE
    # ---------------------------------------------------------

    score = min(score, 100)

    if not reasons:
        reasons.append("No strong signal")

    return score, " | ".join(reasons)


async def check_solana_tokens() -> List[Dict]:

    alerts = []

    pairs = await fetch_solana_pairs()

    for pair in pairs:

        data = get_pair_data(pair)

        if not data:
            continue

        address = data["address"]

        if not address:
            continue

        liquidity = data["liquidity"]

        if liquidity < MIN_LIQUIDITY_USD:
            continue

        state.record_token(address, data)

        if not state.can_alert(address):
            continue

        score, reasons = score_token(data)

        logger.info(
            f"Solana {data['symbol']}: "
            f"{score:.0f}/100 - {reasons}"
        )

        if score >= ALERT_THRESHOLD:

            alerts.append({
                **data,
                "score": score,
                "reasons": reasons,
            })

            state.record_alert(address)

    return alerts


async def send_telegram_alert(alert: Dict):

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing")
        return

    if not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID is missing")
        return

    try:

        bot = Bot(token=TELEGRAM_BOT_TOKEN)

        symbol = alert["symbol"]
        name = alert["name"]
        score = alert["score"]
        reasons = alert["reasons"]

        liquidity = alert["liquidity"]
        volume = alert["volume_24h"]
        market_cap = alert["market_cap"]
        change = alert["price_change_24h"]

        timestamp = datetime.utcnow().strftime("%H:%M UTC")

        message = f"""
🎯 *SOLANA NARRATIVE ALERT*

🪙 *{symbol}* — {name}

🔥 Score: `{score:.0f}/100`
⏰ {timestamp}

📊 *Market Data*

💧 Liquidity: `${liquidity:,.0f}`
📈 Volume 24h: `${volume:,.0f}`
💎 Market Cap: `${market_cap:,.0f}`
📊 Change 24h: `{change:.1f}%`

✨ *Signals*

{reasons}

🔗 [View on DexScreener]({alert["url"]})
"""

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="Markdown",
        )

        logger.info(
            f"✅ Telegram alert sent: {symbol}"
        )

    except Exception as e:
        logger.error(
            f"Telegram error: {e}"
        )


async def main():

    logger.info(
        "🚀 Solana Narrative Bot started"
    )

    logger.info(
        f"Alert threshold: {ALERT_THRESHOLD}/100"
    )

    logger.info(
        f"Check interval: {CHECK_INTERVAL_MINUTES} minutes"
    )

    while True:

        try:

            logger.info("=" * 70)

            logger.info(
                f"Checking... "
                f"{datetime.utcnow().strftime('%H:%M:%S')} UTC"
            )

            alerts = await check_solana_tokens()

            if alerts:

                logger.info(
                    f"🎯 {len(alerts)} alert(s) triggered"
                )

                for alert in alerts:
                    await send_telegram_alert(alert)

            else:

                logger.info(
                    "✓ No alerts this check"
                )

            logger.info(
                f"Sleeping for "
                f"{CHECK_INTERVAL_MINUTES} minutes..."
            )

            await asyncio.sleep(
                CHECK_INTERVAL_MINUTES * 60
            )

        except Exception as e:

            logger.error(
                f"Main loop error: {e}",
                exc_info=True
            )

            await asyncio.sleep(60)


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped"
        )
