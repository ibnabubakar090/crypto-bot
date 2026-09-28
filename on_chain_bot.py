#!/usr/bin/env python3

"""
Solana Narrative Tracking Bot

Uses DexScreener for market data
and Telegram for alerts.
"""

import os
import time
import logging
import asyncio
from datetime import datetime
from typing import Optional, Dict, List, Tuple

import httpx
from telegram import Bot


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# SETTINGS
# ============================================================

DEXSCREENER_API_BASE = "https://api.dexscreener.com/latest/dex"

ALERT_THRESHOLD = 65

MIN_COOLDOWN_HOURS = 4

CHECK_INTERVAL_MINUTES = 10

MIN_LIQUIDITY_USD = 50_000

# Only consider pairs created within this many hours
NEW_PAIR_MAX_AGE_HOURS = 72


# ============================================================
# MAJOR TOKENS TO IGNORE
# ============================================================

IGNORED_SYMBOLS = {
    "SOL",
    "WSOL",
    "USDC",
    "USDT",
    "BTC",
    "WBTC",
    "ETH",
    "WETH",
    "DAI",
    "USDE",
    "FDUSD",
}


# ============================================================
# BOT STATE
# ============================================================

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

        elapsed = (
            time.time()
            - self.alert_history[address]
        )

        return elapsed > MIN_COOLDOWN_HOURS * 3600

    def record_alert(self, address: str):

        self.alert_history[address] = time.time()


state = BotState()


# ============================================================
# PAIR AGE
# ============================================================

def get_pair_age_hours(pair_created_at) -> Optional[float]:

    """
    DexScreener pairCreatedAt is normally a Unix timestamp
    in milliseconds.
    """

    if not pair_created_at:

        return None

    try:

        created_ms = float(pair_created_at)

        now_ms = time.time() * 1000

        age_hours = (
            now_ms - created_ms
        ) / (1000 * 60 * 60)

        return max(age_hours, 0)

    except Exception:

        return None


# ============================================================
# FETCH SOLANA PAIRS
# ============================================================

async def fetch_solana_pairs() -> List[Dict]:

    """
    Search DexScreener for Solana pairs.

    The search endpoint is not a perfect global feed of every
    newly-created Solana pair, so we search several terms and
    then aggressively filter the results.
    """

    queries = [
        "AI",
        "agent",
        "meme",
        "dog",
        "cat",
        "pepe",
        "frog",
        "game",
        "gaming",
        "DePIN",
        "RWA",
        "Solana",
        "pump",
        "coin",
        "token",
    ]

    all_pairs = {}

    try:

        async with httpx.AsyncClient(
            timeout=20,
            headers={
                "User-Agent": "Solana-Narrative-Bot/1.0"
            }
        ) as client:

            for query in queries:

                try:

                    response = await client.get(
                        f"{DEXSCREENER_API_BASE}/search",
                        params={"q": query},
                    )

                    response.raise_for_status()

                    data = response.json()

                    for pair in data.get("pairs", []):

                        if pair.get("chainId") != "solana":
                            continue

                        pair_address = pair.get(
                            "pairAddress"
                        )

                        if not pair_address:
                            continue

                        all_pairs[pair_address] = pair

                except Exception as e:

                    logger.warning(
                        f"Search failed for '{query}': {e}"
                    )

            pairs = list(all_pairs.values())

            logger.info(
                f"📊 DexScreener: Found "
                f"{len(pairs)} unique Solana pairs"
            )

            return pairs

    except Exception as e:

        logger.error(
            f"DexScreener fetch error: {e}"
        )

        return []


# ============================================================
# PARSE PAIR
# ============================================================

def get_pair_data(pair: Dict) -> Optional[Dict]:

    try:

        base_token = pair.get(
            "baseToken",
            {}
        )

        symbol = (
            base_token.get("symbol")
            or "UNKNOWN"
        )

        name = (
            base_token.get("name")
            or "Unknown"
        )

        address = (
            base_token.get("address")
            or ""
        )

        liquidity = float(
            pair.get(
                "liquidity",
                {}
            ).get(
                "usd",
                0
            ) or 0
        )

        volume_24h = float(
            pair.get(
                "volume",
                {}
            ).get(
                "h24",
                0
            ) or 0
        )

        volume_6h = float(
            pair.get(
                "volume",
                {}
            ).get(
                "h6",
                0
            ) or 0
        )

        volume_1h = float(
            pair.get(
                "volume",
                {}
            ).get(
                "h1",
                0
            ) or 0
        )

        price_change_24h = float(
            pair.get(
                "priceChange",
                {}
            ).get(
                "h24",
                0
            ) or 0
        )

        price_change_6h = float(
            pair.get(
                "priceChange",
                {}
            ).get(
                "h6",
                0
            ) or 0
        )

        price_change_1h = float(
            pair.get(
                "priceChange",
                {}
            ).get(
                "h1",
                0
            ) or 0
        )

        market_cap = float(
            pair.get(
                "marketCap",
                0
            ) or 0
        )

        fdv = float(
            pair.get(
                "fdv",
                0
            ) or 0
        )

        price = float(
            pair.get(
                "priceUsd",
                0
            ) or 0
        )

        pair_created_at = pair.get(
            "pairCreatedAt"
        )

        pair_age_hours = get_pair_age_hours(
            pair_created_at
        )

        return {

            "symbol": symbol,

            "name": name,

            "address": address,

            "pair_address": pair.get(
                "pairAddress",
                ""
            ),

            "price": price,

            "liquidity": liquidity,

            "volume_24h": volume_24h,

            "volume_6h": volume_6h,

            "volume_1h": volume_1h,

            "price_change_24h":
                price_change_24h,

            "price_change_6h":
                price_change_6h,

            "price_change_1h":
                price_change_1h,

            "market_cap":
                market_cap,

            "fdv":
                fdv,

            "pair_created_at":
                pair_created_at,

            "pair_age_hours":
                pair_age_hours,

            "dex":
                pair.get(
                    "dexId",
                    "unknown"
                ),

            "url":
                pair.get(
                    "url",
                    (
                        "https://dexscreener.com/"
                        "solana/"
                        f"{pair.get('pairAddress', '')}"
                    )
                ),
        }

    except Exception as e:

        logger.error(
            f"Pair parsing error: {e}"
        )

        return None


# ============================================================
# FILTER
# ============================================================

def is_candidate(data: Dict) -> bool:

    symbol = (
        data["symbol"]
        .upper()
        .strip()
    )

    name = (
        data["name"]
        .upper()
        .strip()
    )

    liquidity = data["liquidity"]

    age = data["pair_age_hours"]

    # Ignore major/stable tokens
    if symbol in IGNORED_SYMBOLS:

        return False

    # Ignore tokens whose name is obviously a major asset
    if name in {
        "SOLANA",
        "USD COIN",
        "TETHER",
        "WRAPPED SOLANA",
        "WRAPPED BITCOIN",
        "WRAPPED ETHER",
    }:

        return False

    # Require minimum liquidity
    if liquidity < MIN_LIQUIDITY_USD:

        return False

    # Need valid creation time
    if age is None:

        return False

    # Focus on relatively new pairs
    if age > NEW_PAIR_MAX_AGE_HOURS:

        return False

    return True


# ============================================================
# SCORING
# ============================================================

def score_token(
    data: Dict
) -> Tuple[float, str]:

    score = 0

    reasons = []

    age = data["pair_age_hours"]

    liquidity = data["liquidity"]

    volume_24h = data["volume_24h"]

    volume_6h = data["volume_6h"]

    volume_1h = data["volume_1h"]

    change_24h = data["price_change_24h"]

    change_6h = data["price_change_6h"]

    change_1h = data["price_change_1h"]

    market_cap = data["market_cap"]


    # ========================================================
    # AGE
    # ========================================================

    if age <= 1:

        score += 20

        reasons.append(
            f"🆕 Pair age {age:.1f}h"
        )

    elif age <= 6:

        score += 17

        reasons.append(
            f"🆕 Pair age {age:.1f}h"
        )

    elif age <= 12:

        score += 14

        reasons.append(
            f"🆕 Pair age {age:.1f}h"
        )

    elif age <= 24:

        score += 10

        reasons.append(
            f"📅 Pair age {age:.1f}h"
        )

    elif age <= 72:

        score += 5

        reasons.append(
            f"📅 Pair age {age:.1f}h"
        )


    # ========================================================
    # LIQUIDITY
    # ========================================================

    if liquidity >= 1_000_000:

        score += 15

        reasons.append(
            f"💧 Liquidity ${liquidity:,.0f}"
        )

    elif liquidity >= 500_000:

        score += 13

        reasons.append(
            f"💧 Liquidity ${liquidity:,.0f}"
        )

    elif liquidity >= 250_000:

        score += 10

        reasons.append(
            f"💧 Liquidity ${liquidity:,.0f}"
        )

    elif liquidity >= 100_000:

        score += 7

        reasons.append(
            f"💧 Liquidity ${liquidity:,.0f}"
        )

    else:

        score += 3

        reasons.append(
            f"💧 Liquidity ${liquidity:,.0f}"
        )


    # ========================================================
    # VOLUME / LIQUIDITY
    # ========================================================

    if liquidity > 0:

        volume_ratio = (
            volume_24h / liquidity
        )

    else:

        volume_ratio = 0


    if volume_ratio >= 10:

        score += 20

        reasons.append(
            f"🔥 Extreme activity "
            f"{volume_ratio:.1f}x"
        )

    elif volume_ratio >= 5:

        score += 17

        reasons.append(
            f"🔥 Very high activity "
            f"{volume_ratio:.1f}x"
        )

    elif volume_ratio >= 2:

        score += 13

        reasons.append(
            f"📈 High activity "
            f"{volume_ratio:.1f}x"
        )

    elif volume_ratio >= 1:

        score += 9

        reasons.append(
            f"📊 Strong activity "
            f"{volume_ratio:.1f}x"
        )

    elif volume_ratio >= 0.5:

        score += 5

        reasons.append(
            f"📊 Moderate activity "
            f"{volume_ratio:.1f}x"
        )


    # ========================================================
    # SHORT-TERM VOLUME
    # ========================================================

    if volume_1h > 100_000:

        score += 5

        reasons.append(
            f"⚡ 1h volume ${volume_1h:,.0f}"
        )

    elif volume_1h > 25_000:

        score += 3

        reasons.append(
            f"⚡ Active 1h volume"
        )


    # ========================================================
    # MOMENTUM
    # ========================================================

    if change_1h >= 50:

        score += 10

        reasons.append(
            f"🚀 1h +{change_1h:.0f}%"
        )

    elif change_1h >= 20:

        score += 7

        reasons.append(
            f"📈 1h +{change_1h:.0f}%"
        )

    elif change_1h > 0:

        score += 3

        reasons.append(
            f"📊 1h +{change_1h:.0f}%"
        )


    if change_6h >= 100:

        score += 10

        reasons.append(
            f"🚀 6h +{change_6h:.0f}%"
        )

    elif change_6h >= 50:

        score += 7

        reasons.append(
            f"🚀 6h +{change_6h:.0f}%"
        )

    elif change_6h >= 20:

        score += 4

        reasons.append(
            f"📈 6h +{change_6h:.0f}%"
        )


    if change_24h >= 100:

        score += 10

        reasons.append(
            f"🚀 24h +{change_24h:.0f}%"
        )

    elif change_24h >= 50:

        score += 7

        reasons.append(
            f"📈 24h +{change_24h:.0f}%"
        )

    elif change_24h >= 20:

        score += 4

        reasons.append(
            f"📈 24h +{change_24h:.0f}%"
        )


    # ========================================================
    # MARKET CAP
    # ========================================================

    if 0 < market_cap <= 10_000_000:

        score += 10

        reasons.append(
            f"💎 Micro-cap ${market_cap:,.0f}"
        )

    elif market_cap <= 50_000_000:

        score += 8

        reasons.append(
            f"💎 Small-cap ${market_cap:,.0f}"
        )

    elif market_cap <= 100_000_000:

        score += 6

        reasons.append(
            f"💎 Low-cap ${market_cap:,.0f}"
        )

    elif market_cap <= 500_000_000:

        score += 3

        reasons.append(
            f"📊 Mid-cap ${market_cap:,.0f}"
        )


    # ========================================================
    # PENALTY FOR HEAVY DECLINE
    # ========================================================

    if change_1h <= -30:

        score -= 10

        reasons.append(
            f"⛔ 1h decline {change_1h:.0f}%"
        )

    elif change_6h <= -40:

        score -= 8

        reasons.append(
            f"⛔ 6h decline {change_6h:.0f}%"
        )


    score = max(
        0,
        min(score, 100)
    )


    if not reasons:

        reasons.append(
            "No strong signal"
        )


    return (
        score,
        " | ".join(reasons)
    )


# ============================================================
# CHECK TOKENS
# ============================================================

async def check_solana_tokens() -> List[Dict]:

    alerts = []

    pairs = await fetch_solana_pairs()

    candidates = 0

    for pair in pairs:

        data = get_pair_data(pair)

        if not data:

            continue

        address = data["address"]

        if not address:

            continue

        if not is_candidate(data):

            continue

        candidates += 1

        state.record_token(
            address,
            data
        )

        if not state.can_alert(address):

            continue

        score, reasons = score_token(
            data
        )

        logger.info(
            f"🪙 {data['symbol']} "
            f"| age={data['pair_age_hours']:.1f}h "
            f"| score={score:.0f}/100 "
            f"| liq=${data['liquidity']:,.0f}"
        )

        if score >= ALERT_THRESHOLD:

            alerts.append({
                **data,
                "score": score,
                "reasons": reasons,
            })

            state.record_alert(
                address
            )


    logger.info(
        f"🔎 Candidates after filtering: "
        f"{candidates}"
    )

    return alerts


# ============================================================
# TELEGRAM
# ============================================================

async def send_telegram_alert(
    alert: Dict
):

    if not TELEGRAM_BOT_TOKEN:

        logger.error(
            "TELEGRAM_BOT_TOKEN is missing"
        )

        return

    if not TELEGRAM_CHAT_ID:

        logger.error(
            "TELEGRAM_CHAT_ID is missing"
        )

        return


    try:

        bot = Bot(
            token=TELEGRAM_BOT_TOKEN
        )

        symbol = alert["symbol"]

        name = alert["name"]

        score = alert["score"]

        reasons = alert["reasons"]

        liquidity = alert["liquidity"]

        volume = alert["volume_24h"]

        market_cap = alert["market_cap"]

        change = alert["price_change_24h"]

        age = alert["pair_age_hours"]

        timestamp = datetime.utcnow().strftime(
            "%H:%M UTC"
        )


        # Avoid Markdown formatting problems from
        # unusual token names/symbols.
        message = (
            "🎯 SOLANA NARRATIVE ALERT\n\n"

            f"🪙 {symbol} — {name}\n\n"

            f"🔥 Score: {score:.0f}/100\n"

            f"⏰ Detected: {timestamp}\n"

            f"🆕 Pair age: {age:.1f} hours\n\n"

            "📊 MARKET DATA\n\n"

            f"💧 Liquidity: "
            f"${liquidity:,.0f}\n"

            f"📈 Volume 24h: "
            f"${volume:,.0f}\n"

            f"💎 Market Cap: "
            f"${market_cap:,.0f}\n"

            f"📊 Change 24h: "
            f"{change:.1f}%\n\n"

            "✨ SIGNALS\n\n"

            f"{reasons}\n\n"

            f"🔗 {alert['url']}"
        )


        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message
        )


        logger.info(
            f"✅ Telegram alert sent: "
            f"{symbol}"
        )


    except Exception as e:

        logger.error(
            f"Telegram error: {e}"
        )


# ============================================================
# MAIN LOOP
# ============================================================

async def main():

    logger.info(
        "🚀 Solana Narrative Bot started"
    )

    logger.info(
        f"Alert threshold: "
        f"{ALERT_THRESHOLD}/100"
    )

    logger.info(
        f"New pair window: "
        f"{NEW_PAIR_MAX_AGE_HOURS} hours"
    )

    logger.info(
        f"Minimum liquidity: "
        f"${MIN_LIQUIDITY_USD:,}"
    )

    logger.info(
        f"Check interval: "
        f"{CHECK_INTERVAL_MINUTES} minutes"
    )


    while True:

        try:

            logger.info(
                "=" * 70
            )

            logger.info(
                "🔍 Checking Solana market..."
            )

            alerts = await check_solana_tokens()


            if alerts:

                logger.info(
                    f"🎯 {len(alerts)} alert(s) triggered"
                )

                for alert in alerts:

                    await send_telegram_alert(
                        alert
                    )

            else:

                logger.info(
                    "✓ No alerts this check"
                )


            logger.info(
                f"😴 Sleeping for "
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

            await asyncio.sleep(
                60
            )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "🛑 Bot stopped"
        )
