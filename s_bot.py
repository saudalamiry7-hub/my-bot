“””
╔══════════════════════════════════════════════════════════════════════════════╗
║          Liquidity Rotation Scanner v3 Professional                        ║
║          ملف واحد كامل — جاهز للنسخ والتشغيل مباشرة                      ║
║                                                                            ║
║  المصادر: CoinGecko + Binance Public API (مجاني بالكامل)                  ║
║  الأبعاد: Liquidity | Smart Money | Volume | Order Flow | Sector | Health  ║
║  التنبيهات: Telegram Bot مع نظام Cooldown ذكي                             ║
║  المراقبة: Binance WebSocket للعملات عالية الدرجة                         ║
╚══════════════════════════════════════════════════════════════════════════════╝

طريقة التشغيل:
python liquidity_scanner_v3.py          ← تشغيل مستمر
python liquidity_scanner_v3.py –test   ← اختبار الاتصالات فقط
python liquidity_scanner_v3.py –once   ← دورة واحدة ثم توقف
“””

# ══════════════════════════════════════════════════════════════════════════════

# المكتبات

# ══════════════════════════════════════════════════════════════════════════════

import os
import sys
import json
import time
import math
import logging
import argparse
import threading
import traceback
import statistics
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from collections import defaultdict, deque
from typing import Optional

import websocket   # pip install websocket-client

# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────┐

# │  SECTION 1 — الإعدادات                                                 │

# │  ⬇️  ضع بيانات Telegram هنا قبل التشغيل                               │

# └─────────────────────────────────────────────────────────────────────────┘

# ══════════════════════════════════════════════════════════════════════════════

CONFIG = {

```
# ══════════════════════════════════════════
#  🤖 TELEGRAM — ضع بياناتك هنا
# ══════════════════════════════════════════
#
#  كيف تحصل على BOT_TOKEN:
#    1. افتح Telegram وابحث عن @BotFather
#    2. اكتب /newbot واتبع الخطوات
#    3. ستحصل على token بهذا الشكل: 7123456789:AAFxxxxxxxx
#
#  كيف تحصل على CHAT_ID:
#    1. أرسل أي رسالة لبوتك
#    2. افتح هذا الرابط في المتصفح:
#       https://api.telegram.org/bot<TOKEN>/getUpdates
#    3. ابحث عن "chat":{"id": XXXXXXX}

"TELEGRAM_BOT_TOKEN": "8509548153:AAEdsqKFuALjrTEgU8f8wExvm2fIf1Y9dig",
"TELEGRAM_CHAT_ID":   "873875241",

# ══════════════════════════════════════════
#  📡 مصادر البيانات (لا تعدّل هذا القسم)
# ══════════════════════════════════════════
"COINGECKO_BASE":    "https://api.coingecko.com/api/v3",
"BINANCE_REST_BASE": "https://api.binance.com",
"BINANCE_FAPI_BASE": "https://fapi.binance.com",
"BINANCE_WS_BASE":   "wss://stream.binance.com:9443",

# ══════════════════════════════════════════
#  🔍 نطاق الفحص
# ══════════════════════════════════════════
"TOP_N_COINS":       500,
"SCAN_INTERVAL_SEC": 1500,    # 25 دقيقة

# ══════════════════════════════════════════
#  💰 فلاتر الماركت كاب
# ══════════════════════════════════════════
"MC_MID_MIN":          80_000_000,    # 80M  الهدف الرئيسي
"MC_MID_MAX":        1_000_000_000,   # 1B
"MC_SMALL_MIN":        20_000_000,    # 20M  فلتر ثانوي
"MC_SMALL_MAX":        79_999_999,
"SMALL_CAP_MIN_SCORE": 3.9,           # درجة أدنى لـ Small Cap

# ══════════════════════════════════════════
#  🎯 عتبات التنبيه
# ══════════════════════════════════════════
"ALERT_MIN":    3.5,    # أدنى درجة لإرسال تنبيه
"ALERT_STRONG": 4.0,    # إشارة قوية 🟠
"ALERT_ULTRA":  4.5,    # إشارة استثنائية 🔴

# ══════════════════════════════════════════
#  ⏱️ نظام التبريد (Cooldown)
# ══════════════════════════════════════════
"COOLDOWN_MODERATE_MIN": 45,   # دقيقة — للإشارات 3.5-3.9
"COOLDOWN_STRONG_MIN":   20,   # دقيقة — للإشارات 4.0-4.4
"COOLDOWN_ULTRA_MIN":    10,   # دقيقة — للإشارات 4.5+
"SCORE_JUMP_OVERRIDE":   0.3,  # ارتفع التقييم بهذا المقدار → تنبيه فوري

# ══════════════════════════════════════════
#  📊 WebSocket
# ══════════════════════════════════════════
"WS_WATCH_MIN_SCORE":   3.0,   # عملات ≥ 3.0 تُراقَب لحظياً
"WS_PRICE_SPIKE_PCT":   3.0,   # تحرك % يستدعي تنبيهاً
"WS_VOLUME_SPIKE_MULT": 3.0,   # حجم × X عن المعتاد = spike
"WS_MAX_SYMBOLS":       50,    # أقصى عملات في WebSocket

# ══════════════════════════════════════════
#  🔄 Rate Limiting (لا تعدّل)
# ══════════════════════════════════════════
"BINANCE_REQUEST_DELAY":   0.12,   # ثانية بين طلبات Binance
"COINGECKO_REQUEST_DELAY": 2.0,    # ثانية بين طلبات CoinGecko
"MAX_RETRIES":             4,
"RETRY_BASE_DELAY":        3,

# ══════════════════════════════════════════
#  📈 إعدادات التحليل
# ══════════════════════════════════════════
"KLINES_DAYS":      30,    # أيام Klines
"ORDER_BOOK_DEPTH": 20,    # عمق Order Book

# ══════════════════════════════════════════
#  🗂️ السجلات
# ══════════════════════════════════════════
"LOG_FILE":  "scanner.log",
"LOG_LEVEL": "INFO",
```

}

# ══════════════════════════════════════════════════════════════════════════════

# إعداد السجلات

# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
level=getattr(logging, CONFIG[“LOG_LEVEL”], logging.INFO),
format=”%(asctime)s [%(levelname)s] %(name)s — %(message)s”,
handlers=[
logging.FileHandler(CONFIG[“LOG_FILE”], encoding=“utf-8”),
logging.StreamHandler(sys.stdout),
]
)
log = logging.getLogger(“LRS”)

# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────┐

# │  SECTION 2 — هياكل البيانات (Models)                                   │

# └─────────────────────────────────────────────────────────────────────────┘

# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CoinData:
“”“البيانات الكاملة لكل عملة بعد الجمع من CoinGecko + Binance”””

```
# تعريف
id:     str
symbol: str
name:   str
rank:   int

# بيانات السوق (CoinGecko)
price:            float
market_cap:       float
volume_24h:       float
price_change_1h:  float
price_change_24h: float
price_change_7d:  float
ath:              float
atl:              float

# بيانات Binance OHLCV
volume_7d_avg:      float = 0.0
volume_30d_avg:     float = 0.0
volume_spike_ratio: float = 0.0
price_volatility:   float = 0.0

# بيانات Order Book
bid_ask_ratio:  float = 0.0
book_imbalance: float = 0.0
buy_wall_usd:   float = 0.0
sell_wall_usd:  float = 0.0

# بيانات Futures
funding_rate:       float = 0.0
funding_rate_avg7d: float = 0.0
open_interest:      float = 0.0
oi_change_24h:      float = 0.0

# بيانات On-Chain / Community
dev_score:           float = 0.0
community_score:     float = 0.0
commit_acceleration: float = 0.0

# تصنيف وجودة
sector:              str  = "Other"
has_binance_spot:    bool = False
has_binance_futures: bool = False
binance_symbol:      str  = ""
```

@dataclass
class DimensionScore:
“”“نتيجة بُعد تحليلي واحد”””
name:       str
score:      float          # 0.0 → 1.0
weight:     float
signals:    list = field(default_factory=list)
sub_scores: dict = field(default_factory=dict)

@dataclass
class CoinAnalysis:
“”“التحليل الكامل لعملة”””
coin:           CoinData
dimensions:     list
final_score:    float
confidence:     str
risk_level:     str
data_quality:   str
entry_scenario: str
stop_loss_pct:  float
tp1_pct:        float
tp2_pct:        float
timestamp:      datetime = field(default_factory=datetime.utcnow)

```
def score_label(self) -> str:
    s = self.final_score
    if s >= 4.5: return "🔴 ULTRA"
    if s >= 4.0: return "🟠 STRONG"
    if s >= 3.5: return "🟡 MODERATE"
    return "⚪ WATCH"
```

# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────┐

# │  SECTION 3 — جلب البيانات (Data Fetcher)                               │

# └─────────────────────────────────────────────────────────────────────────┘

# ══════════════════════════════════════════════════════════════════════════════

# ── تصنيف القطاعات ──────────────────────────────────────────────────────────

SECTOR_MAP = {
“AI”:      [“ai”, “artificial”, “intelligence”, “neural”, “fetch”, “ocean”,
“singularity”, “render”, “akash”, “gpt”, “agi”, “bittensor”],
“RWA”:     [“rwa”, “ondo”, “centrifuge”, “maple”, “goldfinch”, “creditcoin”,
“landx”, “backed”],
“DeFi”:    [“defi”, “swap”, “finance”, “yield”, “liquidity”, “protocol”,
“curve”, “aave”, “compound”, “uniswap”, “dydx”, “gmx”, “pendle”],
“Gaming”:  [“game”, “gaming”, “play”, “metaverse”, “nft”, “axie”, “sandbox”,
“decentraland”, “gala”, “illuvium”, “imx”, “beam”, “ronin”],
“Meme”:    [“doge”, “shib”, “pepe”, “floki”, “bonk”, “meme”, “wojak”,
“inu”, “elon”, “moon”, “cat”, “frog”],
“Layer1”:  [“avalanche”, “solana”, “near”, “aptos”, “sui”, “sei”,
“injective”, “cosmos”, “algorand”],
“Layer2”:  [“optimism”, “arbitrum”, “polygon”, “zk”, “rollup”, “blast”,
“base”, “scroll”, “starknet”, “linea”],
“Oracle”:  [“oracle”, “chainlink”, “band”, “api3”, “uma”, “tellor”, “pyth”],
“Liquid Staking”: [“lido”, “rocket”, “frax”, “ankr”, “eigen”, “restaking”],
“Storage”: [“filecoin”, “arweave”, “sia”, “storj”],
}

def classify_sector(coin_id: str, symbol: str, name: str) -> str:
text = f”{coin_id} {symbol} {name}”.lower()
for sector, keywords in SECTOR_MAP.items():
if any(kw in text for kw in keywords):
return sector
return “Other”

# ── HTTP Helper ──────────────────────────────────────────────────────────────

def _http_get(url: str, params: dict = None, delay: float = 0.0,
source: str = “API”) -> Optional[dict | list]:
“”“GET مع Retry وExponential Backoff”””
if delay > 0:
time.sleep(delay)
for attempt in range(CONFIG[“MAX_RETRIES”]):
try:
r = requests.get(url, params=params,
headers={“accept”: “application/json”}, timeout=15)
if r.status_code == 429:
wait = CONFIG[“RETRY_BASE_DELAY”] * (2 ** attempt) + 5
log.warning(f”[{source}] Rate limit → انتظار {wait:.0f}s”)
time.sleep(wait)
continue
if r.status_code == 404:
return None
r.raise_for_status()
return r.json()
except requests.Timeout:
log.warning(f”[{source}] Timeout (attempt {attempt+1})”)
except requests.RequestException as e:
log.warning(f”[{source}] Error (attempt {attempt+1}): {e}”)
time.sleep(CONFIG[“RETRY_BASE_DELAY”] * (2 ** attempt))
log.error(f”[{source}] فشل بعد {CONFIG[‘MAX_RETRIES’]} محاولات”)
return None

# ── CoinGecko ────────────────────────────────────────────────────────────────

class CoinGeckoFetcher:

```
BASE  = CONFIG["COINGECKO_BASE"]
DELAY = CONFIG["COINGECKO_REQUEST_DELAY"]

def fetch_markets_page(self, page: int, per_page: int = 250) -> list[dict]:
    data = _http_get(
        f"{self.BASE}/coins/markets",
        params={
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": per_page, "page": page,
            "sparkline": "false",
            "price_change_percentage": "1h,24h,7d",
        },
        delay=self.DELAY if page > 1 else 0,
        source="CoinGecko"
    )
    return data or []

def fetch_top500(self) -> list[dict]:
    log.info("📡 [CoinGecko] جلب أول 500 عملة...")
    p1 = self.fetch_markets_page(1, 250)
    p2 = self.fetch_markets_page(2, 250)
    result = p1 + p2
    log.info(f"✅ [CoinGecko] جُلب {len(result)} عملة")
    return result

def fetch_coin_detail(self, coin_id: str) -> Optional[dict]:
    return _http_get(
        f"{self.BASE}/coins/{coin_id}",
        params={
            "localization": "false", "tickers": "false",
            "market_data": "false", "community_data": "true",
            "developer_data": "true", "sparkline": "false",
        },
        delay=self.DELAY, source="CoinGecko-Detail"
    )
```

# ── Binance REST ─────────────────────────────────────────────────────────────

class BinanceFetcher:

```
SPOT  = CONFIG["BINANCE_REST_BASE"]
FAPI  = CONFIG["BINANCE_FAPI_BASE"]
DELAY = CONFIG["BINANCE_REQUEST_DELAY"]

# رموز خاصة لا تتبع النمط العادي
SYMBOL_OVERRIDES = {
    "bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "binancecoin": "BNBUSDT",
    "ripple": "XRPUSDT", "solana": "SOLUSDT", "dogecoin": "DOGEUSDT",
    "cardano": "ADAUSDT", "tron": "TRXUSDT", "avalanche-2": "AVAXUSDT",
    "shiba-inu": "SHIBUSDT", "chainlink": "LINKUSDT", "polkadot": "DOTUSDT",
    "near": "NEARUSDT", "uniswap": "UNIUSDT", "litecoin": "LTCUSDT",
    "stellar": "XLMUSDT", "injective-protocol": "INJUSDT",
    "sei-network": "SEIUSDT", "render-token": "RENDERUSDT",
    "fetch-ai": "FETUSDT", "ondo-finance": "ONDOUSDT",
    "pendle": "PENDLEUSDT", "arbitrum": "ARBUSDT",
    "optimism": "OPUSDT", "starknet": "STRKUSDT",
    "blur": "BLURUSDT", "jupiter-ag": "JUPUSDT",
}

def spot_symbol(self, coin_id: str, symbol: str) -> str:
    return self.SYMBOL_OVERRIDES.get(coin_id, f"{symbol.upper()}USDT")

def fetch_klines(self, symbol: str, interval: str = "1d",
                 limit: int = 30) -> list:
    return _http_get(
        f"{self.SPOT}/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        delay=self.DELAY, source="Binance-Klines"
    ) or []

def fetch_order_book(self, symbol: str) -> Optional[dict]:
    return _http_get(
        f"{self.SPOT}/api/v3/depth",
        params={"symbol": symbol, "limit": CONFIG["ORDER_BOOK_DEPTH"]},
        delay=self.DELAY, source="Binance-OrderBook"
    )

def fetch_funding_history(self, symbol: str, limit: int = 56) -> list:
    return _http_get(
        f"{self.FAPI}/fapi/v1/fundingRate",
        params={"symbol": symbol, "limit": limit},
        delay=self.DELAY, source="Binance-Funding"
    ) or []

def fetch_open_interest(self, symbol: str) -> Optional[dict]:
    return _http_get(
        f"{self.FAPI}/fapi/v1/openInterest",
        params={"symbol": symbol},
        delay=self.DELAY, source="Binance-OI"
    )

def fetch_oi_history(self, symbol: str) -> list:
    return _http_get(
        f"{self.FAPI}/futures/data/openInterestHist",
        params={"symbol": symbol, "period": "1d", "limit": 30},
        delay=self.DELAY, source="Binance-OI-History"
    ) or []
```

# ── Data Enricher ─────────────────────────────────────────────────────────────

class DataEnricher:
“”“يجمع CoinGecko + Binance في CoinData واحد”””

```
def __init__(self):
    self.cg = CoinGeckoFetcher()
    self.bn = BinanceFetcher()

def _volume_stats(self, klines: list) -> dict:
    if len(klines) < 2:
        return {}
    volumes = [float(k[5]) for k in klines]
    closes  = [float(k[4]) for k in klines]
    returns = [(closes[i] - closes[i-1]) / closes[i-1]
               for i in range(1, len(closes)) if closes[i-1] > 0]

    vol_7d    = statistics.mean(volumes[-7:])  if len(volumes) >= 7  else 0
    vol_30d   = statistics.mean(volumes[-30:]) if len(volumes) >= 30 else statistics.mean(volumes)
    vol_today = volumes[-1] if volumes else 0
    spike     = vol_today / vol_30d if vol_30d > 0 else 1.0
    volatility = statistics.stdev(returns) if len(returns) >= 2 else 0.0

    return {"volume_7d_avg": vol_7d, "volume_30d_avg": vol_30d,
            "volume_spike_ratio": spike, "price_volatility": volatility}

def _book_stats(self, book: dict) -> dict:
    if not book:
        return {}
    bids = [(float(p), float(q)) for p, q in book.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in book.get("asks", [])]
    total_b = sum(p * q for p, q in bids)
    total_a = sum(p * q for p, q in asks)
    total   = total_b + total_a
    return {
        "bid_ask_ratio":  total_b / total_a if total_a > 0 else 1.0,
        "book_imbalance": (total_b - total_a) / total if total > 0 else 0.0,
        "buy_wall_usd":   max((p * q for p, q in bids), default=0),
        "sell_wall_usd":  max((p * q for p, q in asks), default=0),
    }

def _funding_stats(self, history: list) -> dict:
    if not history:
        return {}
    rates   = [float(h["fundingRate"]) * 100 for h in history]
    current = rates[-1] if rates else 0.0
    avg_7d  = statistics.mean(rates[-21:]) if len(rates) >= 3 else current
    return {"funding_rate": current, "funding_rate_avg7d": avg_7d}

def _oi_stats(self, oi_now: Optional[dict], oi_hist: list) -> dict:
    if not oi_now:
        return {}
    oi_current = float(oi_now.get("openInterest", 0))
    oi_change  = 0.0
    if len(oi_hist) >= 2:
        vals    = [float(h.get("sumOpenInterest", 0)) for h in oi_hist]
        oi_old  = vals[-2]
        if oi_old > 0:
            oi_change = (vals[-1] - oi_old) / oi_old * 100
    return {"open_interest": oi_current, "oi_change_24h": oi_change}

def _dev_community(self, detail: Optional[dict]) -> dict:
    if not detail:
        return {}
    dev  = detail.get("developer_data", {}) or {}
    comm = detail.get("community_data",  {}) or {}

    c4           = dev.get("commit_count_4_weeks", 0) or 0
    commit_accel = min(1.0, c4 / 50) if c4 > 0 else 0.0
    pull_req     = dev.get("pull_request_contributors", 0) or 0
    stars        = dev.get("stars", 0) or 0
    forks        = dev.get("forks", 0) or 0
    dev_score    = min(100, commit_accel * 40 + min(40, pull_req * 0.5)
                      + min(20, (stars + forks) / 1000))

    def log_norm(x, cap=1_000_000):
        return min(1.0, math.log10(max(x, 1)) / math.log10(cap))

    twitter  = comm.get("twitter_followers", 0) or 0
    reddit   = comm.get("reddit_subscribers", 0) or 0
    telegram = comm.get("telegram_channel_user_count", 0) or 0
    comm_score = min(100, (log_norm(twitter, 5_000_000) * 40
                           + log_norm(reddit, 500_000)   * 30
                           + log_norm(telegram, 200_000) * 30) * 100)

    return {"dev_score": dev_score, "community_score": comm_score,
            "commit_acceleration": commit_accel}

def enrich(self, raw: dict) -> CoinData:
    coin_id = raw.get("id", "")
    symbol  = raw.get("symbol", "").upper()
    name    = raw.get("name", "")

    coin = CoinData(
        id=coin_id, symbol=symbol, name=name,
        rank=raw.get("market_cap_rank", 999) or 999,
        price=float(raw.get("current_price") or 0),
        market_cap=float(raw.get("market_cap") or 0),
        volume_24h=float(raw.get("total_volume") or 0),
        price_change_1h=float(raw.get("price_change_percentage_1h_in_currency") or 0),
        price_change_24h=float(raw.get("price_change_percentage_24h") or 0),
        price_change_7d=float(raw.get("price_change_percentage_7d_in_currency") or 0),
        ath=float(raw.get("ath") or 0),
        atl=float(raw.get("atl") or 1e-10),
        sector=classify_sector(coin_id, symbol, name),
    )

    bn_sym = self.bn.spot_symbol(coin_id, symbol)
    coin.binance_symbol = bn_sym

    # Klines
    klines = self.bn.fetch_klines(bn_sym, "1d", CONFIG["KLINES_DAYS"])
    if klines:
        coin.has_binance_spot = True
        s = self._volume_stats(klines)
        coin.volume_7d_avg      = s.get("volume_7d_avg", 0)
        coin.volume_30d_avg     = s.get("volume_30d_avg", 0)
        coin.volume_spike_ratio = s.get("volume_spike_ratio", 1.0)
        coin.price_volatility   = s.get("price_volatility", 0)

    # Order Book
    book = self.bn.fetch_order_book(bn_sym)
    if book:
        s = self._book_stats(book)
        coin.bid_ask_ratio  = s.get("bid_ask_ratio", 1.0)
        coin.book_imbalance = s.get("book_imbalance", 0.0)
        coin.buy_wall_usd   = s.get("buy_wall_usd", 0.0)
        coin.sell_wall_usd  = s.get("sell_wall_usd", 0.0)

    # Futures
    funding = self.bn.fetch_funding_history(bn_sym)
    if funding:
        coin.has_binance_futures = True
        s = self._funding_stats(funding)
        coin.funding_rate       = s.get("funding_rate", 0)
        coin.funding_rate_avg7d = s.get("funding_rate_avg7d", 0)

    oi_now  = self.bn.fetch_open_interest(bn_sym)
    oi_hist = self.bn.fetch_oi_history(bn_sym)
    if oi_now:
        s = self._oi_stats(oi_now, oi_hist)
        coin.open_interest = s.get("open_interest", 0)
        coin.oi_change_24h = s.get("oi_change_24h", 0)

    return coin

def build_list(self, raw_list: list) -> list[CoinData]:
    total  = len(raw_list)
    result = []
    for i, raw in enumerate(raw_list, 1):
        try:
            result.append(self.enrich(raw))
            if i % 10 == 0:
                log.info(f"  ↳ إثراء البيانات: {i}/{total}")
        except Exception as e:
            log.warning(f"خطأ في إثراء {raw.get('symbol','?')}: {e}")
    log.info(f"✅ اكتمل إثراء {len(result)} عملة")
    return result
```

# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────┐

# │  SECTION 4 — محرك التقييم (Scanner — 6 أبعاد)                         │

# └─────────────────────────────────────────────────────────────────────────┘

# ══════════════════════════════════════════════════════════════════════════════

# ── Sector Tracker ────────────────────────────────────────────────────────────

class SectorMomentumTracker:

```
def __init__(self):
    self._changes: dict[str, list[float]] = defaultdict(list)
    self._volumes: dict[str, list[float]] = defaultdict(list)
    self._market_avg = 0.0

def update(self, coins: list[CoinData]):
    self._changes.clear()
    self._volumes.clear()
    all_ch = []
    for c in coins:
        self._changes[c.sector].append(c.price_change_24h)
        self._volumes[c.sector].append(c.volume_24h)
        all_ch.append(c.price_change_24h)
    self._market_avg = statistics.mean(all_ch) if all_ch else 0.0

def relative_strength(self, sector: str) -> float:
    data = self._changes.get(sector, [])
    if not data:
        return 0.30
    relative = statistics.mean(data) - self._market_avg
    return round(max(0.0, min(1.0, 1 / (1 + math.exp(-relative / 3)))), 4)

def sector_breadth(self, sector: str) -> float:
    data = self._changes.get(sector, [])
    if not data:
        return 0.20
    positive = sum(1 for x in data if x > 2.0)
    return min(1.0, positive / len(data) * 1.5)

def volume_surge(self, sector: str, coin_volume: float) -> float:
    vols = self._volumes.get(sector, [])
    if not vols or coin_volume == 0:
        return 0.30
    avg   = statistics.mean(vols)
    ratio = coin_volume / avg if avg > 0 else 1.0
    return min(1.0, math.log10(max(ratio, 0.1) + 1) / math.log10(6))
```

SECTOR_TRACKER = SectorMomentumTracker()

# ── البُعد 1: Liquidity Zones & Heatmap (20%) ────────────────────────────────

class D1_LiquidityZones:
WEIGHT = 0.20

```
def score(self, c: CoinData) -> DimensionScore:
    signals, sub = [], {}

    # Order Block Zone
    s1 = 0.0
    if c.ath > 0 and c.price > 0:
        d = (c.ath - c.price) / c.ath
        if   0.50 <= d <= 0.80: s1 = max(0.0, 1.0 - abs(d - 0.65) * 3); signals.append(f"OB Zone ✓ ({d*100:.0f}% below ATH)")
        elif 0.80 <  d <= 0.95: s1 = 0.70; signals.append(f"Deep Value ({d*100:.0f}% below ATH)")
        elif 0.20 <= d <  0.50: s1 = 0.40
        elif d < 0.10:          s1 = 0.15; signals.append("Near ATH — resistance risk")
    sub["order_block"] = round(s1, 3)

    # Fair Value Gap
    s2 = 0.0
    if c.price_change_24h != 0:
        ratio = abs(c.price_change_1h) / (abs(c.price_change_24h) + 0.01)
        if ratio > 0.25:
            s2 = min(1.0, ratio * 1.5)
            signals.append(f"FVG Signal (1h={c.price_change_1h:+.1f}% / 24h={c.price_change_24h:+.1f}%)")
        if c.price_change_7d < -10 and c.price_change_1h > 1.0:
            s2 = max(s2, 0.65)
            signals.append(f"FVG Bounce (7d={c.price_change_7d:.1f}%)")
    sub["fvg"] = round(s2, 3)

    # Short Squeeze Zone
    s3 = 0.0
    if c.price_change_7d <= -20:
        s3 = 0.50 + min(0.50, abs(c.price_change_7d + 20) / 30)
        signals.append(f"Short Squeeze Candidate ({c.price_change_7d:.1f}% 7d)")
    elif -20 < c.price_change_7d <= -10:
        s3 = 0.35
    sub["squeeze_zone"] = round(s3, 3)

    # ATL Accumulation Zone
    s4 = 0.0
    if c.atl > 0 and c.price > 0:
        mult = c.price / c.atl
        if   2.0 <= mult <= 5.0: s4 = 0.70; signals.append(f"Accumulation Zone ({mult:.1f}x above ATL)")
        elif 1.05 <= mult < 2.0: s4 = 0.85; signals.append("Near ATL — Squeeze risk/reward")
        elif mult > 10:          s4 = 0.30
    sub["atl_zone"] = round(s4, 3)

    final = round(min(1.0, s1*0.35 + s2*0.25 + s3*0.25 + s4*0.15), 4)
    return DimensionScore("Liquidity Zones & Heatmap", final, self.WEIGHT, signals, sub)
```

# ── البُعد 2: Smart Money / Order Flow (20%) ─────────────────────────────────

class D2_SmartMoney:
WEIGHT = 0.20

```
def score(self, c: CoinData) -> DimensionScore:
    signals, sub = [], {}

    # Order Book Imbalance
    s1 = 0.0
    if c.has_binance_spot and c.book_imbalance != 0:
        imb = c.book_imbalance
        if   imb > 0.15:  s1 = min(1.0, 0.60 + imb * 1.5); signals.append(f"Buy Pressure: {imb:+.2f}")
        elif imb > 0.05:  s1 = 0.55
        elif imb >= -0.05: s1 = 0.40
        else:              s1 = max(0.0, 0.30 + imb); signals.append(f"Sell Pressure: {imb:+.2f}")
    else:
        s1 = min(0.60, c.volume_24h / max(c.market_cap, 1) * 2)
    sub["book_imbalance"] = round(s1, 3)

    # Absorption Signal
    s2 = 0.0
    if c.price_change_24h < -5 and c.volume_spike_ratio > 1.5:
        s2 = min(1.0, (abs(c.price_change_24h) / 15) * (c.volume_spike_ratio / 3))
        signals.append(f"Absorption ✓ ({c.price_change_24h:.1f}% / {c.volume_spike_ratio:.1f}x vol)")
    elif c.price_change_24h > 5 and c.volume_spike_ratio > 2.0:
        s2 = min(1.0, 0.50 + (c.volume_spike_ratio - 2) * 0.15)
        signals.append(f"Volume Breakout ✓ ({c.volume_spike_ratio:.1f}x)")
    sub["absorption"] = round(s2, 3)

    # Funding Rate Divergence
    s3 = 0.35
    if c.has_binance_futures:
        dev = c.funding_rate - c.funding_rate_avg7d
        if   dev < -0.03: s3 = min(1.0, abs(dev) * 20); signals.append(f"Negative Funding Divergence ({dev:+.4f}%)")
        elif dev >  0.05: s3 = 0.25; signals.append(f"High Positive Funding ({dev:+.4f}%) — caution")
        else:              s3 = 0.40
    sub["funding_divergence"] = round(s3, 3)

    # OI Change
    s4 = 0.35
    if c.has_binance_futures and c.open_interest > 0:
        oi = c.oi_change_24h
        if   5 <= oi <= 30: s4 = min(1.0, oi / 30); signals.append(f"OI Inflow +{oi:.1f}%")
        elif oi > 30:        s4 = 0.50; signals.append(f"OI Spike {oi:.1f}% — speculative")
        elif oi < -10:       s4 = 0.20; signals.append(f"OI Outflow {oi:.1f}%")
        else:                s4 = 0.35
    sub["oi_change"] = round(s4, 3)

    final = round(min(1.0, s1*0.30 + s2*0.30 + s3*0.25 + s4*0.15), 4)
    return DimensionScore("Smart Money / Order Flow", final, self.WEIGHT, signals, sub)
```

# ── البُعد 3: Volume Confirmation (25%) ──────────────────────────────────────

class D3_VolumeConfirmation:
WEIGHT = 0.25

```
def score(self, c: CoinData) -> DimensionScore:
    signals, sub = [], {}

    # Volume Spike vs 7d & 30d
    s1 = 0.0
    if c.volume_30d_avg > 0:
        sp30 = c.volume_24h / c.volume_30d_avg
        sp7  = c.volume_24h / c.volume_7d_avg if c.volume_7d_avg > 0 else 1.0
        if   2.0 <= sp30 <= 6.0: s1 = 0.60 + min(0.40, (sp30 - 2) / 4 * 0.40); signals.append(f"Vol Spike ✓ {sp30:.1f}x (30d avg)")
        elif 1.5 <= sp30 < 2.0:  s1 = 0.50
        elif sp30 > 6.0:         s1 = 0.45; signals.append(f"Extreme Vol {sp30:.1f}x — caution")
        else:                    s1 = max(0.0, sp30 * 0.20)
        if sp7 > 1.8 and sp30 > 1.8:
            s1 = min(1.0, s1 + 0.10)
            signals.append(f"Vol Consistency ✓ (7d={sp7:.1f}x / 30d={sp30:.1f}x)")
    else:
        s1 = min(0.55, c.volume_24h / max(c.market_cap, 1) * 1.5)
    sub["volume_spike"] = round(s1, 3)

    # VCP Pattern
    s2 = 0.0
    if c.price_volatility > 0:
        if   c.price_volatility < 0.03 and c.volume_spike_ratio > 2.0:
            s2 = min(1.0, (2 - c.price_volatility * 30) * (c.volume_spike_ratio / 4))
            signals.append(f"VCP Pattern ✓ (vol={c.price_volatility*100:.1f}% / spike={c.volume_spike_ratio:.1f}x)")
        elif 0.03 <= c.price_volatility <= 0.06: s2 = 0.45
        else: s2 = max(0.0, 0.30 - (c.price_volatility - 0.06) * 2)
    sub["vcp"] = round(s2, 3)

    # Volume/Price Confirmation
    s3 = 0.35
    if   c.price_change_24h > 3 and c.volume_spike_ratio > 1.5:
        s3 = min(1.0, (c.price_change_24h / 15) * (c.volume_spike_ratio / 3))
        signals.append("Vol/Price Confirm ✓")
    elif c.price_change_24h > 3 and c.volume_spike_ratio < 1.0:
        s3 = 0.20; signals.append("Price up / Vol weak — unconfirmed")
    elif c.price_change_24h < -3 and c.volume_spike_ratio < 0.8:
        s3 = 0.55; signals.append("Exhaustion Sell — low vol drop")
    sub["price_vol_confirm"] = round(s3, 3)

    s4 = 0.40   # Relative Volume Rank — يُضبط في ScanEngine
    sub["relative_rank"] = round(s4, 3)

    final = round(min(1.0, s1*0.40 + s2*0.25 + s3*0.25 + s4*0.10), 4)
    return DimensionScore("Volume Confirmation", final, self.WEIGHT, signals, sub)
```

# ── البُعد 4: Order Flow & Clusters (15%) ────────────────────────────────────

class D4_OrderFlowClusters:
WEIGHT = 0.15

```
def score(self, c: CoinData) -> DimensionScore:
    signals, sub = [], {}

    # Buy Wall Dominance
    s1 = 0.0
    total_w = c.buy_wall_usd + c.sell_wall_usd
    if total_w > 0:
        buy_dom = c.buy_wall_usd / total_w
        if   buy_dom > 0.65: s1 = min(1.0, buy_dom * 1.3); signals.append(f"Buy Wall {buy_dom*100:.0f}%")
        elif buy_dom > 0.50: s1 = 0.55
        else:                s1 = max(0.0, buy_dom); signals.append(f"Sell Wall Dominant")
    sub["wall_dominance"] = round(s1, 3)

    # Bid/Ask Ratio
    s2 = 0.0
    if c.bid_ask_ratio > 0:
        r = c.bid_ask_ratio
        if   r > 1.3:        s2 = min(1.0, 0.60 + (r - 1.3) * 0.5); signals.append(f"Bid/Ask={r:.2f} — Buy Pressure")
        elif 0.9 <= r <= 1.3: s2 = 0.45
        else:                 s2 = max(0.10, r * 0.40); signals.append(f"Bid/Ask={r:.2f} — Sell Pressure")
    sub["bid_ask"] = round(s2, 3)

    # Funding + OI Alignment
    s3 = 0.35
    if c.has_binance_futures:
        if   c.funding_rate < -0.01 and c.oi_change_24h > 5:
            s3 = min(1.0, 0.70 + abs(c.funding_rate) * 10 + c.oi_change_24h / 100)
            signals.append(f"Short Squeeze Setup ✓ (FR={c.funding_rate:.4f} / OI={c.oi_change_24h:+.1f}%)")
        elif c.funding_rate > 0.05 and c.oi_change_24h < -5:
            s3 = 0.25; signals.append("Long Squeeze Risk")
        elif abs(c.funding_rate) < 0.01:
            s3 = 0.50
    sub["funding_oi"] = round(s3, 3)

    # Fibonacci Cluster
    s4 = 0.30
    if c.ath > c.atl > 0:
        pct     = (c.price - c.atl) / (c.ath - c.atl)
        min_d   = min(abs(pct - f) for f in [0.236, 0.382, 0.500, 0.618, 0.786])
        if   min_d < 0.03: s4 = min(1.0, 0.70 + (0.03 - min_d) * 15); signals.append(f"Near Fib Level ({pct*100:.1f}%)")
        elif min_d < 0.07: s4 = 0.50
    sub["fib_cluster"] = round(s4, 3)

    final = round(min(1.0, s1*0.30 + s2*0.25 + s3*0.30 + s4*0.15), 4)
    return DimensionScore("Order Flow & Clusters", final, self.WEIGHT, signals, sub)
```

# ── البُعد 5: Sector Momentum (12%) ──────────────────────────────────────────

class D5_SectorMomentum:
WEIGHT = 0.12

```
def score(self, c: CoinData) -> DimensionScore:
    signals = []
    rs  = SECTOR_TRACKER.relative_strength(c.sector)
    brd = SECTOR_TRACKER.sector_breadth(c.sector)
    vs  = SECTOR_TRACKER.volume_surge(c.sector, c.volume_24h)

    if rs  > 0.70: signals.append(f"Sector [{c.sector}] outperforming market")
    if brd > 0.60: signals.append(f"Broad [{c.sector}] rally ({brd*100:.0f}% coins up)")
    if vs  > 0.70: signals.append("Above-sector volume surge")

    final = round(min(1.0, rs*0.45 + brd*0.35 + vs*0.20), 4)
    return DimensionScore("Sector Momentum", final, self.WEIGHT, signals,
                          {"relative_strength": round(rs, 3),
                           "sector_breadth": round(brd, 3),
                           "volume_surge": round(vs, 3)})
```

# ── البُعد 6: On-Chain Health & Funding (8%) ──────────────────────────────────

class D6_OnChainHealth:
WEIGHT = 0.08

```
def score(self, c: CoinData) -> DimensionScore:
    signals, sub = [], {}

    # Developer Activity
    s1 = 0.0
    if c.dev_score > 0:
        s1 = min(1.0, c.dev_score / 80)
        if c.commit_acceleration > 0.5:
            s1 = min(1.0, s1 + 0.20)
            signals.append(f"Dev Acceleration ✓ (score={c.dev_score:.0f})")
        else:
            signals.append(f"Dev Activity: {c.dev_score:.0f}/100")
    sub["dev"] = round(s1, 3)

    # Community Score
    s2 = min(1.0, c.community_score / 75) if c.community_score > 0 else 0.0
    if s2 > 0: signals.append(f"Community: {c.community_score:.0f}/100")
    sub["community"] = round(s2, 3)

    # Funding Sentiment
    s3 = 0.35
    if c.has_binance_futures:
        fr = c.funding_rate
        if   -0.05 < fr < -0.01: s3 = 0.70; signals.append(f"Healthy Negative Funding ({fr:.4f}%)")
        elif -0.10 < fr <= -0.05: s3 = 0.85; signals.append(f"Strong Negative Funding — Squeeze setup")
        elif 0 <= fr <= 0.02:     s3 = 0.55
        elif fr > 0.05:           s3 = 0.20; signals.append(f"High Positive Funding — risky")
    sub["funding"] = round(s3, 3)

    # OI Trend
    s4 = 0.35
    if c.has_binance_futures and c.open_interest > 0:
        oi = c.oi_change_24h
        if 3 <= oi <= 20: s4 = min(1.0, 0.55 + oi / 40); signals.append(f"OI Growing +{oi:.1f}%")
        elif oi < -5:     s4 = 0.20
    sub["oi_trend"] = round(s4, 3)

    final = round(min(1.0, s1*0.30 + s2*0.20 + s3*0.30 + s4*0.20), 4)
    return DimensionScore("On-Chain Health & Funding", final, self.WEIGHT, signals, sub)
```

# ── Scan Engine ───────────────────────────────────────────────────────────────

class ScanEngine:

```
def __init__(self):
    self.d1 = D1_LiquidityZones()
    self.d2 = D2_SmartMoney()
    self.d3 = D3_VolumeConfirmation()
    self.d4 = D4_OrderFlowClusters()
    self.d5 = D5_SectorMomentum()
    self.d6 = D6_OnChainHealth()

def _data_quality(self, c: CoinData) -> str:
    if c.has_binance_spot and c.has_binance_futures: return "✅ FULL"
    if c.has_binance_spot:                           return "⚠️ PARTIAL"
    return "❌ LIMITED"

def _confidence(self, score: float, quality: str) -> str:
    penalty = quality == "❌ LIMITED"
    for thr, lbl in [(4.5, "ULTRA"), (4.0, "HIGH"), (3.5, "MEDIUM"), (0, "LOW")]:
        if score >= thr:
            if penalty and lbl in ("ULTRA", "HIGH"):
                lbl = {"ULTRA": "HIGH", "HIGH": "MEDIUM"}[lbl]
            return lbl
    return "LOW"

def _risk(self, c: CoinData) -> str:
    if (c.price_volatility > 0.06 or c.market_cap < CONFIG["MC_SMALL_MAX"]
            or abs(c.funding_rate) > 0.08):
        return "HIGH"
    if (c.price_volatility > 0.03 or c.market_cap < 200_000_000
            or abs(c.funding_rate) > 0.04):
        return "MEDIUM"
    return "LOW"

def _entry(self, c: CoinData, dims: list) -> str:
    if dims[0].score > 0.70 and dims[1].score > 0.65:
        return "دخول عند تثبيت سعري فوق Order Block مع تأكيد حجم"
    if dims[2].score > 0.75:
        return "دخول على Breakout مؤكد بحجم — SL تحت قاع آخر 4 شمعات"
    if any("Squeeze" in s for d in dims[:2] for s in d.signals):
        return "دخول تدريجي في منطقة التراكم — استهداف Squeeze قصير الأجل"
    if any("Absorption" in s for s in dims[1].signals):
        return "دخول بعد Absorption confirmation — انتظار شمعة انعكاس"
    return "دخول عند اختبار منطقة الدعم مع تأكيد حجم"

def _sl_tp(self, c: CoinData, risk: str, score: float):
    vol = c.price_volatility if c.price_volatility > 0 else 0.03
    sl  = max(3.0, min(15.0, vol * 150))
    if risk == "HIGH":
        sl = min(15.0, sl * 1.3)
    rr1 = 1.5 + (score - 3.5) * 0.5
    rr2 = 2.5 + (score - 3.5) * 1.0
    return round(sl, 1), round(sl * rr1, 1), round(sl * rr2, 1)

def _make_analysis(self, coin: CoinData, dims: list) -> CoinAnalysis:
    weighted = sum(d.score * d.weight for d in dims)
    final    = round(weighted * 5, 2)
    quality  = self._data_quality(coin)
    risk     = self._risk(coin)
    sl, tp1, tp2 = self._sl_tp(coin, risk, final)
    return CoinAnalysis(
        coin=coin, dimensions=dims, final_score=final,
        confidence=self._confidence(final, quality),
        risk_level=risk, data_quality=quality,
        entry_scenario=self._entry(coin, dims),
        stop_loss_pct=sl, tp1_pct=tp1, tp2_pct=tp2,
    )

def scan_all(self, coins: list[CoinData]) -> list[CoinAnalysis]:
    # حساب أبعاد كل العملات
    raw = []
    for c in coins:
        dims = [self.d1.score(c), self.d2.score(c), self.d3.score(c),
                self.d4.score(c), self.d5.score(c), self.d6.score(c)]
        raw.append((c, dims))

    # ضبط Relative Volume Rank
    max_spike = max((c.volume_spike_ratio for c, _ in raw), default=1.0) or 1.0
    for c, dims in raw:
        rank_score = round(c.volume_spike_ratio / max_spike, 3)
        dims[2].sub_scores["relative_rank"] = rank_score
        dims[2].score = round(min(1.0, dims[2].score * 0.90 + rank_score * 0.10), 4)

    results = [self._make_analysis(c, dims) for c, dims in raw]
    return sorted(results, key=lambda x: x.final_score, reverse=True)
```

# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────┐

# │  SECTION 5 — نظام التنبيهات (Alert Manager)                            │

# └─────────────────────────────────────────────────────────────────────────┘

# ══════════════════════════════════════════════════════════════════════════════

class CooldownManager:

```
def __init__(self):
    self._history: dict[str, dict] = {}

def _cooldown(self, score: float) -> int:
    if score >= CONFIG["ALERT_ULTRA"]:   return CONFIG["COOLDOWN_ULTRA_MIN"]
    if score >= CONFIG["ALERT_STRONG"]:  return CONFIG["COOLDOWN_STRONG_MIN"]
    return CONFIG["COOLDOWN_MODERATE_MIN"]

def should_alert(self, symbol: str, score: float) -> tuple[bool, str]:
    entry = self._history.get(symbol)
    if entry is None:
        return True, "first_alert"
    elapsed = (datetime.utcnow() - entry["time"]).total_seconds() / 60
    if elapsed >= self._cooldown(entry["score"]):
        return True, "cooldown_expired"
    if score >= entry["score"] + CONFIG["SCORE_JUMP_OVERRIDE"]:
        return True, f"score_jump +{score - entry['score']:.1f}"
    return False, f"cooldown ({elapsed:.0f}/{self._cooldown(entry['score'])}min)"

def record(self, symbol: str, score: float):
    self._history[symbol] = {"score": score, "time": datetime.utcnow()}
```

class MessageFormatter:

```
RISK  = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}
CONF  = {"LOW": "○", "MEDIUM": "◐", "HIGH": "●", "ULTRA": "⬤"}

def _mc(self, mc: float) -> str:
    if mc >= 1e9: return f"${mc/1e9:.2f}B"
    if mc >= 1e6: return f"${mc/1e6:.0f}M"
    return f"${mc:,.0f}"

def _bar(self, score: float, width: int = 10) -> str:
    f = round(score / 5 * width)
    return "█" * f + "░" * (width - f)

def format_alert(self, a: CoinAnalysis, reason: str = "") -> str:
    c   = a.coin
    now = datetime.utcnow().strftime("%H:%M UTC")
    lines = [
        f"{a.score_label()}",
        f"{'━'*34}",
        f"💎 <b>{c.name}</b>  (<code>{c.symbol}</code>)",
        f"📊 MC: <b>{self._mc(c.market_cap)}</b>  |  رتبة #{c.rank}",
        f"💰 السعر: <b>${c.price:,.4g}</b>",
        f"",
        f"⚡ قوة الإشارة:  <b>{a.final_score:.1f} / 5.0</b>",
        f"   {self._bar(a.final_score)}",
        f"",
        f"📐 <b>الأبعاد:</b>",
    ]
    for d in a.dimensions:
        bar = "■" * round(d.score * 5) + "□" * (5 - round(d.score * 5))
        lines.append(f"  {bar} {d.score:.2f} — {d.name}")
    lines += [
        f"",
        f"🎯 الثقة: {self.CONF.get(a.confidence,'○')} <b>{a.confidence}</b>   "
        f"⚠️ المخاطرة: {self.RISK.get(a.risk_level,'🔴')} <b>{a.risk_level}</b>",
        f"🗂️ البيانات: {a.data_quality}",
        f"",
        f"📍 <b>سيناريو الدخول:</b>",
        f"   {a.entry_scenario}",
        f"",
        f"🛡️ <b>إدارة المخاطرة:</b>",
        f"   SL: -{a.stop_loss_pct:.1f}%   TP1: +{a.tp1_pct:.1f}%   TP2: +{a.tp2_pct:.1f}%",
        f"",
        f"🏷️ القطاع: <b>{c.sector}</b>",
        f"📈 1h {c.price_change_1h:+.1f}%  |  24h {c.price_change_24h:+.1f}%  |  7d {c.price_change_7d:+.1f}%",
    ]
    # أبرز الإشارات
    top = [s for d in a.dimensions for s in d.signals[:1]][:4]
    if top:
        lines += ["", "🔍 <b>أبرز الإشارات:</b>"]
        for s in top:
            lines.append(f"   • {s}")
    lines += [
        f"",
        f"{'━'*34}",
        f"🕐 {now}  |  <i>LRS v3 Pro</i>",
    ]
    if "score_jump" in reason:
        lines.append("⬆️ <i>تحديث: ارتفاع ملحوظ في التقييم</i>")
    return "\n".join(lines)

def format_summary(self, sent: list[CoinAnalysis],
                   duration: float, total: int) -> str:
    now = datetime.utcnow().strftime("%H:%M UTC")
    lines = [
        f"📋 <b>ملخص دورة الفحص</b>",
        f"{'━'*28}",
        f"🕐 {now}  |  فُحص: {total} عملة",
        f"⏱️ المدة: {duration:.0f}s  |  تنبيهات: {len(sent)}",
    ]
    if sent:
        lines += ["", "🏆 <b>أقوى 5 إشارات:</b>"]
        for i, a in enumerate(sent[:5], 1):
            lines.append(f"  {i}. {a.coin.symbol:8} {a.score_label()} — {a.final_score:.1f}")
    lines.append(f"{'━'*28}")
    return "\n".join(lines)
```

class TelegramSender:

```
URL = "https://api.telegram.org/bot{token}/sendMessage"

def __init__(self):
    self.token   = CONFIG["TELEGRAM_BOT_TOKEN"]
    self.chat_id = CONFIG["TELEGRAM_CHAT_ID"]
    if "ضع_" in self.token or "ضع_" in str(self.chat_id):
        log.error("⛔ بيانات Telegram غير مُعدَّة — افتح السكريبت وعدّل TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID")

def send(self, text: str) -> bool:
    url = self.URL.format(token=self.token)
    for attempt in range(3):
        try:
            r = requests.post(url, json={
                "chat_id": self.chat_id, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }, timeout=10)
            if r.status_code == 200:
                return True
            log.warning(f"Telegram [{r.status_code}]: {r.text[:80]}")
        except requests.RequestException as e:
            log.warning(f"Telegram error (attempt {attempt+1}): {e}")
        time.sleep(2 ** attempt)
    return False

def test_connection(self) -> bool:
    ok = self.send("✅ <b>LRS v3 Pro</b> — اتصال ناجح!\nالنظام يعمل وجاهز.")
    log.info("✅ Telegram متصل" if ok else "❌ فشل اتصال Telegram")
    return ok
```

class AlertManager:

```
def __init__(self):
    self.cooldown  = CooldownManager()
    self.formatter = MessageFormatter()
    self.telegram  = TelegramSender()
    self._sent     = 0

def process(self, analyses: list[CoinAnalysis],
            duration: float = 0, total: int = 0) -> list[CoinAnalysis]:
    sent = []
    for a in analyses:
        c, sc = a.coin, a.final_score

        # فلتر الماركت كاب
        in_mid   = CONFIG["MC_MID_MIN"] <= c.market_cap <= CONFIG["MC_MID_MAX"]
        in_small = CONFIG["MC_SMALL_MIN"] <= c.market_cap <= CONFIG["MC_SMALL_MAX"]
        if not (in_mid or in_small): continue
        if in_small and sc < CONFIG["SMALL_CAP_MIN_SCORE"]: continue

        # فلتر العتبة
        if sc < CONFIG["ALERT_MIN"]: break

        # فلتر Cooldown
        ok, reason = self.cooldown.should_alert(c.symbol, sc)
        if not ok:
            log.debug(f"[SKIP] {c.symbol} {sc:.1f} — {reason}")
            continue

        msg = self.formatter.format_alert(a, reason)
        if self.telegram.send(msg):
            self.cooldown.record(c.symbol, sc)
            sent.append(a)
            self._sent += 1
            log.info(f"📨 {c.symbol:8} {a.score_label()} {sc:.1f} ({reason})")
            time.sleep(1.5)

    # ملخص الدورة
    self.telegram.send(self.formatter.format_summary(sent, duration, total))
    return sent

@property
def total_sent(self) -> int:
    return self._sent
```

# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────┐

# │  SECTION 6 — WebSocket Monitor                                         │

# └─────────────────────────────────────────────────────────────────────────┘

# ══════════════════════════════════════════════════════════════════════════════

class LiveCoinState:

```
def __init__(self, symbol: str, baseline_price: float, baseline_vol: float):
    self.symbol         = symbol
    self.baseline_price = baseline_price
    self.baseline_vol   = baseline_vol
    self.current_price  = baseline_price
    self.current_vol    = 0.0
    self.price_hist: deque = deque(maxlen=20)
    self.alert_triggered = False

def update(self, price: float, vol: float):
    self.current_price = price
    self.current_vol   = vol
    self.price_hist.append(price)

def recent_change_pct(self) -> float:
    if len(self.price_hist) < 5: return 0.0
    old = list(self.price_hist)[-5]
    return (self.current_price - old) / old * 100 if old else 0.0

def vol_spike(self) -> float:
    per_min = self.baseline_vol / 1440
    return self.current_vol / per_min if per_min > 0 else 1.0
```

class WebSocketMonitor:

```
WS_URL = CONFIG["BINANCE_WS_BASE"] + "/stream?streams={streams}"

def __init__(self):
    self._states: dict[str, LiveCoinState] = {}
    self._ws     = None
    self._thread = None
    self._running = False
    self._lock    = threading.Lock()
    self._reconnect_delay = 5
    self.on_spike = None   # Callback

def update_watchlist(self, symbol_map: dict[str, tuple[float, float]]):
    limited = dict(list(symbol_map.items())[:CONFIG["WS_MAX_SYMBOLS"]])
    with self._lock:
        for sym, (price, vol) in limited.items():
            sl = sym.lower()
            if sl not in self._states:
                self._states[sl] = LiveCoinState(sym, price, vol)
        to_rm = [s for s in self._states if s.upper() not in limited]
        for s in to_rm:
            del self._states[s]
    log.info(f"[WS] قائمة المراقبة: {len(self._states)} عملة")
    if self._running:
        self.restart()

def _build_url(self) -> str:
    with self._lock:
        syms = list(self._states.keys())
    if not syms: return ""
    return self.WS_URL.format(streams="/".join(f"{s}@miniTicker" for s in syms))

def _on_message(self, ws, message):
    try:
        d    = json.loads(message).get("data", {})
        sym  = d.get("s", "").lower()
        price = float(d.get("c", 0))
        vol   = float(d.get("v", 0))
        with self._lock:
            state = self._states.get(sym)
        if not state: return
        state.update(price, vol)
        self._check_spike(sym, state)
    except Exception:
        pass

def _check_spike(self, sym: str, state: LiveCoinState):
    price_chg = abs(state.recent_change_pct())
    vspike    = state.vol_spike()
    reasons   = []
    if price_chg >= CONFIG["WS_PRICE_SPIKE_PCT"]:  reasons.append(f"price={price_chg:.1f}%")
    if vspike   >= CONFIG["WS_VOLUME_SPIKE_MULT"]: reasons.append(f"vol={vspike:.1f}x")
    if reasons and not state.alert_triggered:
        state.alert_triggered = True
        log.info(f"[WS] ⚡ {sym.upper()} — {', '.join(reasons)}")
        if self.on_spike:
            self.on_spike(sym.upper(), {
                "symbol": sym.upper(), "price": state.current_price,
                "price_chg": state.recent_change_pct(),
                "vol_spike": vspike, "reasons": reasons,
            })
        def reset():
            time.sleep(300)
            state.alert_triggered = False
        threading.Thread(target=reset, daemon=True).start()

def _on_error(self, ws, error): log.warning(f"[WS] {error}")
def _on_open(self, ws):
    self._reconnect_delay = 5
    log.info(f"[WS] ✅ متصل — {len(self._states)} عملة")
def _on_close(self, ws, code, msg):
    log.info(f"[WS] مغلق ({code})")
    if self._running:
        time.sleep(self._reconnect_delay)
        self._reconnect_delay = min(60, self._reconnect_delay * 2)
        self._connect()

def _connect(self):
    url = self._build_url()
    if not url: return
    self._ws = websocket.WebSocketApp(
        url, on_message=self._on_message, on_error=self._on_error,
        on_close=self._on_close, on_open=self._on_open,
    )
    self._ws.run_forever(ping_interval=30, ping_timeout=10)

def start(self):
    if self._running: return
    self._running = True
    self._thread  = threading.Thread(target=self._connect, daemon=True, name="WS")
    self._thread.start()
    log.info("[WS] Monitor بدأ في الخلفية")

def stop(self):
    self._running = False
    if self._ws: self._ws.close()

def restart(self):
    if self._ws: self._ws.close()
    time.sleep(1)

def get_state(self, symbol: str) -> Optional[LiveCoinState]:
    with self._lock:
        return self._states.get(symbol.lower())
```

# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────┐

# │  SECTION 7 — الحلقة الرئيسية (Main)                                    │

# └─────────────────────────────────────────────────────────────────────────┘

# ══════════════════════════════════════════════════════════════════════════════

STABLE_KEYWORDS = [“usd”, “usdt”, “usdc”, “busd”, “dai”, “tusd”, “frax”,
“lusd”, “wrapped”, “staked”, “wbtc”, “weth”, “pax”]

def filter_coins(raw: list[dict]) -> list[dict]:
result = []
for coin in raw:
mc  = float(coin.get(“market_cap”) or 0)
sym = coin.get(“symbol”, “”).lower()
cid = coin.get(“id”, “”).lower()
if any(kw in sym or kw in cid for kw in STABLE_KEYWORDS): continue
in_mid   = CONFIG[“MC_MID_MIN”] <= mc <= CONFIG[“MC_MID_MAX”]
in_small = CONFIG[“MC_SMALL_MIN”] <= mc <= CONFIG[“MC_SMALL_MAX”]
if in_mid or in_small:
result.append(coin)
return result

class LiquidityScanner:

```
def __init__(self):
    log.info("🚀 تهيئة Liquidity Rotation Scanner v3 Pro...")
    self.cg      = CoinGeckoFetcher()
    self.enricher = DataEnricher()
    self.engine  = ScanEngine()
    self.alerts  = AlertManager()
    self.ws      = WebSocketMonitor()
    self.ws.on_spike = self._on_ws_spike
    self._last_analyses: list[CoinAnalysis] = []
    self._cycle = 0
    log.info("✅ جاهز")

def _on_ws_spike(self, symbol: str, data: dict):
    match = next((a for a in self._last_analyses
                  if a.coin.binance_symbol == symbol), None)
    if not match: return
    msg = (
        f"⚡ <b>WS Alert: {match.coin.name}</b> (<code>{match.coin.symbol}</code>)\n"
        f"{'━'*28}\n"
        f"💰 ${data['price']:,.4g}  ({data['price_chg']:+.1f}%)\n"
        f"📊 حجم: {data['vol_spike']:.1f}x المعتاد\n"
        f"🔍 {', '.join(data['reasons'])}\n"
        f"📋 آخر تقييم: {match.score_label()} {match.final_score:.1f}\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}"
    )
    self.alerts.telegram.send(msg)

def _update_ws(self, analyses: list[CoinAnalysis]):
    watchlist = {}
    for a in analyses:
        if (a.final_score >= CONFIG["WS_WATCH_MIN_SCORE"]
                and a.coin.binance_symbol):
            watchlist[a.coin.binance_symbol] = (
                a.coin.price, a.coin.volume_30d_avg
            )
    top = dict(list(watchlist.items())[:CONFIG["WS_MAX_SYMBOLS"]])
    self.ws.update_watchlist(top)

def run_cycle(self) -> list[CoinAnalysis]:
    self._cycle += 1
    start = time.time()
    log.info(f"\n{'═'*50}")
    log.info(f"🔄 دورة #{self._cycle} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"{'═'*50}")
    try:
        raw      = self.cg.fetch_top500()
        if not raw: return []
        filtered = filter_coins(raw)
        log.info(f"📋 بعد الفلترة: {len(filtered)} عملة مؤهلة")
        coins    = self.enricher.build_list(filtered)
        SECTOR_TRACKER.update(coins)
        log.info("🧠 بدء التقييم...")
        analyses = self.engine.scan_all(coins)
        duration = time.time() - start
        sent     = self.alerts.process(analyses, duration, len(coins))
        self._last_analyses = analyses
        self._update_ws(analyses)
        log.info(f"🏁 دورة #{self._cycle} — {duration:.0f}s — {len(sent)} تنبيه")
        return analyses
    except Exception as e:
        log.error(f"❌ خطأ: {e}\n{traceback.format_exc()}")
        return []

def run_forever(self):
    log.info("♾️ بدء الحلقة الرئيسية...")
    if not self.alerts.telegram.test_connection():
        log.error("⛔ تحقق من بيانات Telegram في أعلى السكريبت")
        sys.exit(1)
    self.ws.start()
    interval = CONFIG["SCAN_INTERVAL_SEC"]
    log.info(f"⏱️ فترة الفحص: {interval//60} دقيقة")
    while True:
        try:
            self.run_cycle()
            log.info(f"💤 انتظار {interval//60} دقيقة...")
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("⛔ إيقاف من المستخدم")
            self.ws.stop()
            break
        except Exception as e:
            log.error(f"❌ خطأ غير متوقع: {e}")
            time.sleep(60)
```

# ── وضع الاختبار ──────────────────────────────────────────────────────────────

def run_test():
log.info(“🧪 وضع الاختبار…”)

```
tg = TelegramSender()
if not tg.test_connection():
    log.error("❌ فشل Telegram — تحقق من TOKEN و CHAT_ID في أعلى السكريبت")
    return

cg  = CoinGeckoFetcher()
raw = cg.fetch_markets_page(1, 5)
log.info(f"✅ CoinGecko: {len(raw)} عملة (اختبار)" if raw else "❌ فشل CoinGecko")

bn = BinanceFetcher()
k  = bn.fetch_klines("BTCUSDT", "1d", 3)
log.info(f"✅ Binance Klines: {len(k)} شمعات" if k else "❌ فشل Binance Klines")

ob = bn.fetch_order_book("BTCUSDT")
log.info(f"✅ Order Book: {len(ob.get('bids',[]))} bids" if ob else "❌ فشل Order Book")

fr = bn.fetch_funding_history("BTCUSDT", 3)
log.info(f"✅ Funding Rate: {fr[-1]['fundingRate'] if fr else 'N/A'}")

log.info("\n✅ الاختبار اكتمل — شغّل بدون --test للبدء الفعلي")
```

# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
parser = argparse.ArgumentParser(description=“Liquidity Rotation Scanner v3 Pro”)
parser.add_argument(”–test”, action=“store_true”, help=“اختبار الاتصالات فقط”)
parser.add_argument(”–once”, action=“store_true”, help=“دورة واحدة ثم توقف”)
args = parser.parse_args()

```
log.info("╔══════════════════════════════════════════╗")
log.info("║  Liquidity Rotation Scanner v3 Pro       ║")
log.info("║  Smart Money | ICT | Whale Detection     ║")
log.info("╚══════════════════════════════════════════╝")

if args.test:
    run_test()
    return

scanner = LiquidityScanner()

if args.once:
    scanner.ws.start()
    scanner.run_cycle()
    scanner.ws.stop()
    return

scanner.run_forever()
```

if **name** == “**main**”:
main()
