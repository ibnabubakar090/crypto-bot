#!/usr/bin/env python3
"""
On-Chain Narrative Tracking Bot
Monitors Solana/Ethereum token launches, whale movements, and volume spikes
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional, Dict, List, Tuple
import asyncio

import httpx
from telegram import Bot
from telegram.error import TelegramError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SOLSCAN_API_BASE = "https://api.solscan.io"
ETHERSCAN_API_BASE = "https://api.etherscan.io/api"
DEXSCREENER_API_BASE = "https://api.dexscreener.com/latest/dex"

ALERT_THRESHOLD = 65
MIN_COOLDOWN_HOURS = 4
CHECK_INTERVAL_MINUTES = 10
MIN_LIQUIDITY_USD = 50000
MAX_AGE_HOURS = 72

class BotState:
    def __init__(self):
        self.seen_tokens = {}
        self.alert_history = {}
        self.volume_history = defaultdict(list)
        self.last_check = 0
        
    def record_token(self, token_address: str, data: Dict):
        self.seen_tokens[token_address] = {
            **data,
            "first_seen": self.seen_tokens.get(token_address, {}).get("first_seen", time.time()),
        }
    
    def record_alert(self, token_address: str):
        self.alert_history[token_address] = time.time()
    
    def can_alert(self, token_address: str) -> bool:
        if token_address not in self.alert_history:
            return True
        last_alert = self.alert_history[token_address]
        cooldown_seconds = MIN_COOLDOWN_HOURS * 3600
        return time.time() - last_alert > cooldown_seconds
    
    def get_token_age_hours(self, token_address: str) -> float:
        if token_address not in self.seen_tokens:
            return 999
        first_seen = self.seen_tokens[token_address].get("first_seen", time.time())
        return (time.time() - first_seen) / 3600

state = BotState()

async def fetch_solana_new_tokens() -> List[Dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{SOLSCAN_API_BASE}/token/list",
                params={
                    "sortBy": "createdTime",
                    "sortOrder": "desc",
                    "pageSize": 50,
                },
            )
            response.raise_for_status()
            data = response.json()
            tokens = data.get("data", {}).get("result", [])
            logger.info(f"📊 Solana: Found {len(tokens)} recent tokens")
            return tokens
    except Exception as e:
        logger.error(f"Solscan fetch error: {e}")
        return []

async def fetch_solana_token_volume(token_address: str) -> Optional[Dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{SOLSCAN_API_BASE}/token/meta",
                params={"token": token_address},
            )
            response.raise_for_status()
            data = response.json()
            if data.get("success"):
                token_data = data.get("data", {})
                return {
                    "price": float(token_data.get("price", 0)),
                    "volume_24h": float(token_data.get("volume24h", 0)),
                    "liquidity": float(token_data.get("liquidity", 0)),
                    "holder_count": int(token_data.get("holder", 0)),
                    "market_cap": float(token_data.get("marketCap", 0)),
                }
            return None
    except Exception as e:
        logger.error(f"Solscan token fetch error: {e}")
        return None

async def fetch_dexscreener_data(token_symbol: str, chain: str = "solana") -> Optional[Dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{DEXSCREENER_API_BASE}/search",
                params={"q": token_symbol},
            )
            response.raise_for_status()
            data = response.json()
            pairs = data.get("pairs", [])
            
            for pair in pairs:
                if pair.get("chainId") == chain or chain in pair.get("dexId", "").lower():
                    return {
                        "price": float(pair.get("priceUsd", 0)),
                        "liquidity": float(pair.get("liquidity", {}).get("usd", 0)),
                        "volume_24h": float(pair.get("volume", {}).get("h24", 0)),
                        "market_cap": float(pair.get("marketCap", 0)),
                        "price_change_24h": float(pair.get("priceChange", {}).get("h24", 0)),
                        "fdv": float(pair.get("fdv", 0)),
                    }
            return None
    except Exception as e:
        logger.error(f"DexScreener fetch error: {e}")
        return None

def score_token(token_data: Dict, market_data: Optional[Dict], chain: str) -> Tuple[float, str]:
    score = 0
    reasons = []
    
    token_address = token_data.get("address", "")
    age_hours = state.get_token_age_hours(token_address)
    
    if age_hours < 1:
        age_score = 15
        reasons.append("🆕 Ultra-fresh (< 1 hour)")
    elif age_hours < 6:
        age_score = 12
        reasons.append("🆕 Fresh (< 6 hours)")
    elif age_hours < 24:
        age_score = 8
        reasons.append("📅 New (< 24 hours)")
    elif age_hours < 72:
        age_score = 3
        reasons.append("📅 Emerging (< 72 hours)")
    else:
        age_score = 0
        reasons.append("⏳ Established (> 72 hours)")
    
    score += age_score
    
    if market_data:
        liquidity = market_data.get("liquidity", 0)
        if liquidity > 1_000_000:
            liq_score = 15
            reasons.append(f"💧 Strong liquidity (${liquidity:,.0f})")
        elif liquidity > 500_000:
            liq_score = 12
            reasons.append(f"💧 Good liquidity (${liquidity:,.0f})")
        elif liquidity > 100_000:
            liq_score = 8
            reasons.append(f"💧 Okay liquidity (${liquidity:,.0f})")
        elif liquidity > MIN_LIQUIDITY_USD:
            liq_score = 3
            reasons.append(f"💧 Minimal liquidity (${liquidity:,.0f})")
        else:
            liq_score = 0
            reasons.append(f"🚨 Too illiquid (${liquidity:,.0f})")
        
        score += liq_score
        
        volume_24h = market_data.get("volume_24h", 0)
        if liquidity > 0:
            vol_liq_ratio = volume_24h / liquidity
            if vol_liq_ratio > 5:
                vol_score = 20
                reasons.append(f"🔥 Extreme volume/liq ratio ({vol_liq_ratio:.1f}x)")
            elif vol_liq_ratio > 2:
                vol_score = 15
                reasons.append(f"🔥 High volume/liq ratio ({vol_liq_ratio:.1f}x)")
            elif vol_liq_ratio > 1:
                vol_score = 10
                reasons.append(f"📈 Good trading activity ({vol_liq_ratio:.1f}x)")
            elif vol_liq_ratio > 0.5:
                vol_score = 5
                reasons.append(f"📊 Moderate activity ({vol_liq_ratio:.1f}x)")
            else:
                vol_score = 0
                reasons.append(f"💤 Low activity ({vol_liq_ratio:.1f}x)")
        else:
            vol_score = 0
        
        score += vol_score
        
        price_change_24h = market_data.get("price_change_24h", 0)
        if price_change_24h > 100:
            momentum_score = 20
            reasons.append(f"🚀 Massive pump (+{price_change_24h:.0f}%)")
        elif price_change_24h > 50:
            momentum_score = 15
            reasons.append(f"🚀 Strong pump (+{price_change_24h:.0f}%)")
        elif price_change_24h > 20:
            momentum_score = 10
            reasons.append(f"📈 Good pump (+{price_change_24h:.0f}%)")
        elif price_change_24h > 0:
            momentum_score = 3
            reasons.append(f"📊 Slight pump (+{price_change_24h:.0f}%)")
        elif price_change_24h > -10:
            momentum_score = 1
            reasons.append(f"⚠️ Slight dump ({price_change_24h:.0f}%)")
        else:
            momentum_score = 0
            reasons.append(f"⛔ Heavy dump ({price_change_24h:.0f}%)")
        
        score += momentum_score
        
        market_cap = market_data.get("market_cap", 0)
        if market_cap > 0 and market_cap < 100_000_000:
            mcap_score = 15
            reasons.append(f"💎 Moonshot potential (${market_cap:,.0f} mcap)")
        elif market_cap < 500_000_000:
            mcap_score = 10
            reasons.append(f"💎 Small cap (${market_cap:,.0f} mcap)")
        elif market_cap < 1_000_000_000:
            mcap_score = 5
            reasons.append(f"📊 Mid cap (${market_cap:,.0f} mcap)")
        else:
            mcap_score = 0
            reasons.append(f"🏛️ Large cap (${market_cap:,.0f} mcap)")
        
        score += mcap_score
        
        holder_count = token_data.get("holder_count", 0)
        if holder_count > 10000:
            holder_score = 15
            reasons.append(f"🤝 Distributed ({holder_count:,} holders)")
        elif holder_count > 1000:
            holder_score = 12
            reasons.append(f"🤝 Decent distribution ({holder_count:,} holders)")
        elif holder_count > 100:
            holder_score = 8
            reasons.append(f"⚠️ Concentrated ({holder_count:,} holders)")
        else:
            holder_score = 0
            reasons.append(f"🚨 Whale heavy ({holder_count:,} holders)")
        
        score += holder_score
    
    score = min(100, score)
    return score, " | ".join(reasons)

async def check_solana_tokens():
    alerts = []
    tokens = await fetch_solana_new_tokens()
    
    for token in tokens:
        token_address = token.get("token", "")
        token_symbol = token.get("symbol", "UNKNOWN")
        token_name = token.get("name", "Unknown")
        
        if not state.can_alert(token_address):
            continue
        
        if token_address in state.seen_tokens:
            age = state.get_token_age_hours(token_address)
            if age > MAX_AGE_HOURS:
                continue
        
        market_data = await fetch_solana_token_volume(token_address)
        if not market_data:
            continue
        
        state.record_token(token_address, {
            "symbol": token_symbol,
            "name": token_name,
            "chain": "solana",
            "address": token_address,
            "holder_count": market_data.get("holder_count", 0),
        })
        
        if market_data.get("liquidity", 0) < MIN_LIQUIDITY_USD:
            continue
        
        score, reasons = score_token(
            state.seen_tokens[token_address],
            market_data,
            "solana"
        )
        
        logger.info(f"Solana {token_symbol}: {score:.0f}/100 - {reasons}")
        
        if score > ALERT_THRESHOLD:
            alerts.append({
                "chain": "solana",
                "symbol": token_symbol,
                "name": token_name,
                "address": token_address,
                "score": score,
                "reasons": reasons,
                "market_data": market_data,
            })
            state.record_alert(token_address)
    
    return alerts

async def send_telegram_alert(alert: Dict):
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        timestamp = datetime.now().strftime("%H:%M UTC")
        chain = alert["chain"].upper()
        symbol = alert["symbol"]
        name = alert["name"]
        score = alert["score"]
        reasons = alert["reasons"]
        market_data = alert["market_data"]
        
        message = f"""
🎯 **{chain} Token Alert**

**{symbol}** - {name}
🔥 Score: `{score:.0f}/100`
⏰ {timestamp}

📊 **Market Data:**
💰 Liquidity: `${market_data['liquidity']:,.0f}`
📈 Volume 24h: `${market_data['volume_24h']:,.0f}`
💎 Market Cap: `${market_data['market_cap']:,.0f}`
📊 Change 24h: `{market_data['price_change_24h']:.1f}%`

✨ **Why it triggered:**
{reasons}

🔗 [DexScreener](https://dexscreener.com/search?q={symbol})
"""
        
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="Markdown",
        )
        logger.info(f"✅ Alert sent for {symbol}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

async def main():
    logger.info("🚀 On-Chain Narrative Bot started")
    logger.info(f"Alert threshold: {ALERT_THRESHOLD}/100")
    logger.info(f"Check interval: {CHECK_INTERVAL_MINUTES} minutes")
    
    while True:
        try:
            logger.info("=" * 70)
            logger.info(f"Checking... {datetime.now().strftime('%H:%M:%S')}")
            
            alerts = await check_solana_tokens()
            
            if alerts:
                logger.info(f"🎯 {len(alerts)} alert(s) triggered")
                for alert in alerts:
                    await send_telegram_alert(alert)
            else:
                logger.info("✓ No alerts this check")
            
            logger.info(f"Sleeping for {CHECK_INTERVAL_MINUTES} minutes...")
            await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            await asyncio.sleep(60)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
