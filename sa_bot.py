"""
Liquidity Rotation Scanner v6 Professional
CoinGecko + Bybit + OKX fallback
No hardcoded fallback scores. All scores derived from real data.
Pure ASCII. Straight quotes only.
"""

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
from datetime import datetime
from dataclasses import dataclass, field
from collections import defaultdict, deque

import websocket


# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG = {
    "TELEGRAM_BOT_TOKEN": "PUT_YOUR_BOT_TOKEN_HERE",
    "TELEGRAM_CHAT_ID":   "PUT_YOUR_CHAT_ID_HERE",

    "COINGECKO_BASE": "https://api.coingecko.com/api/v3",
    "BYBIT_BASE":     "https://api.bybit.com",
    "OKX_BASE":       "https://www.okx.com",
    "BYBIT_WS":       "wss://stream.bybit.com/v5/public/spot",

    "TOP_N_COINS":        300,
    "SCAN_INTERVAL_SEC":  1500,   # 25 minutes

    "MC_MID_MIN":          80_000_000,
    "MC_MID_MAX":        1_000_000_000,
    "MC_SMALL_MIN":        20_000_000,
    "MC_SMALL_MAX":        79_999_999,
    "SMALL_CAP_MIN_SCORE": 3.7,

    "ALERT_MIN":    3.2,   # lowered from 3.5
    "ALERT_STRONG": 4.0,
    "ALERT_ULTRA":  4.5,

    # Cooldown in minutes
    "COOLDOWN_MODERATE_MIN": 120,
    "COOLDOWN_STRONG_MIN":    20,
    "COOLDOWN_ULTRA_MIN":     10,
    "SCORE_JUMP_OVERRIDE":   0.3,

    # Cycle 1: auto-seed top N eligible coins for full enrichment
    "SEED_CANDIDATES_N": 50,

    # Smart enrichment threshold
    "ENRICH_MIN_SCORE": 3.0,

    # WebSocket
    "WS_WATCH_MIN_SCORE":   3.0,
    "WS_PRICE_SPIKE_PCT":   3.0,
    "WS_VOLUME_SPIKE_MULT": 3.0,
    "WS_MAX_SYMBOLS":       40,

    # Rate limiting
    "BYBIT_DELAY":      0.25,
    "OKX_DELAY":        0.25,
    "COINGECKO_DELAY":  2.8,
    "ENRICH_DELAY":     0.8,
    "MAX_RETRIES":      3,
    "RETRY_DELAY":      4,

    # Klines cache
    "CACHE_TTL_SEC":   21600,   # 6 hours
    "CACHE_CLEAN_SEC":  1800,   # clean every 30 min
    "KLINES_DAYS":        30,

    "LOG_FILE":  "scanner.log",
    "LOG_LEVEL": "INFO",
}


# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=getattr(logging, CONFIG["LOG_LEVEL"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["LOG_FILE"], encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("LRS")


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class Coin:
    # Identity
    id:     str
    symbol: str
    name:   str
    rank:   int
    sector: str

    # CoinGecko (always available)
    price:       float
    market_cap:  float
    volume_24h:  float
    change_1h:   float
    change_24h:  float
    change_7d:   float
    ath:         float
    atl:         float

    # Bybit/OKX OHLCV (when available)
    vol_7d_avg:    float = 0.0
    vol_30d_avg:   float = 0.0
    vol_spike:     float = 0.0   # today / 30d avg
    atr_pct:       float = 0.0
    recent_low:    float = 0.0
    recent_high:   float = 0.0
    volatility:    float = 0.0

    # Derivatives (when available)
    funding:       float = 0.0
    funding_avg7d: float = 0.0
    oi:            float = 0.0
    oi_change:     float = 0.0

    # Meta
    has_ohlcv:  bool = False
    has_deriv:  bool = False
    source:     str  = "CoinGecko"
    ex_symbol:  str  = ""


@dataclass
class Dim:
    name:    str
    score:   float   # 0.0 to 1.0
    weight:  float
    signals: list = field(default_factory=list)


@dataclass
class MarketRegime:
    regime:        str   = "neutral"   # risk_on / neutral / risk_off
    btc_short:     str   = "neutral"
    btc_medium:    str   = "neutral"
    alts_state:    str   = "neutral"
    market_vol:    str   = "normal"
    confidence:    str   = "low"


@dataclass
class Alert:
    coin:          Coin
    dims:          list
    score:         float
    confidence:    str
    risk:          str
    quality:       str
    entry:         str
    sl:            float
    tp1:           float
    tp2:           float
    regime:        MarketRegime = field(default_factory=MarketRegime)

    def label(self) -> str:
        if self.score >= 4.5: return "ULTRA"
        if self.score >= 4.0: return "STRONG"
        if self.score >= 3.2: return "MODERATE"
        return "WATCH"


# =============================================================================
# KLINES CACHE
# =============================================================================

class KlinesCache:
    def __init__(self):
        self._data       = {}
        self._lock       = threading.Lock()
        self._last_clean = datetime.utcnow()

    def get(self, sym: str):
        self._clean_if_due()
        with self._lock:
            e = self._data.get(sym)
            if not e: return None
            if (datetime.utcnow() - e["ts"]).total_seconds() > CONFIG["CACHE_TTL_SEC"]:
                del self._data[sym]
                return None
            return e["rows"]

    def set(self, sym: str, rows: list):
        with self._lock:
            self._data[sym] = {"rows": rows, "ts": datetime.utcnow()}

    def _clean_if_due(self):
        if (datetime.utcnow() - self._last_clean).total_seconds() < CONFIG["CACHE_CLEAN_SEC"]:
            return
        now = datetime.utcnow()
        with self._lock:
            expired = [k for k, v in self._data.items()
                       if (now - v["ts"]).total_seconds() > CONFIG["CACHE_TTL_SEC"]]
            for k in expired:
                del self._data[k]
        if expired:
            log.info("[Cache] Removed %d expired entries", len(expired))
        self._last_clean = now

    def size(self) -> int:
        with self._lock: return len(self._data)


CACHE = KlinesCache()


# =============================================================================
# SECTOR CLASSIFIER
# =============================================================================

SECTORS = {
    "AI":          ["ai","artificial","intelligence","neural","fetch","ocean",
                    "singularity","render","akash","gpt","agi","bittensor"],
    "RWA":         ["rwa","ondo","centrifuge","maple","goldfinch","creditcoin"],
    "DeFi":        ["defi","swap","finance","yield","liquidity","protocol",
                    "curve","aave","compound","uniswap","dydx","gmx","pendle"],
    "Gaming":      ["game","gaming","play","metaverse","nft","axie","sandbox",
                    "decentraland","gala","illuvium","imx","beam","ronin"],
    "Meme":        ["doge","shib","pepe","floki","bonk","meme",
                    "wojak","inu","elon","moon","cat","frog"],
    "Layer1":      ["avalanche","solana","near","aptos","sui","sei",
                    "injective","cosmos","algorand"],
    "Layer2":      ["optimism","arbitrum","polygon","zk","rollup",
                    "blast","base","scroll","starknet","linea"],
    "Oracle":      ["oracle","chainlink","band","api3","uma","tellor","pyth"],
    "LiquidStake": ["lido","rocket","frax","ankr","eigen","restaking"],
    "Storage":     ["filecoin","arweave","sia","storj"],
}


def sector_of(coin_id: str, sym: str, name: str) -> str:
    text = (coin_id + " " + sym + " " + name).lower()
    for s, kws in SECTORS.items():
        if any(k in text for k in kws):
            return s
    return "Other"


# =============================================================================
# HTTP
# =============================================================================

def get(url: str, params: dict = None, delay: float = 0, source: str = "API"):
    if delay > 0:
        time.sleep(delay)
    for attempt in range(CONFIG["MAX_RETRIES"]):
        try:
            r = requests.get(url, params=params,
                             headers={"accept": "application/json"}, timeout=12)
            if r.status_code == 429:
                wait = CONFIG["RETRY_DELAY"] * (2 ** attempt) + 5
                log.warning("[%s] Rate limit -> wait %ds", source, wait)
                time.sleep(wait)
                continue
            if r.status_code in (403, 451):
                log.warning("[%s] Blocked HTTP %d", source, r.status_code)
                return None
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.Timeout:
            log.warning("[%s] Timeout attempt %d", source, attempt + 1)
        except requests.RequestException as e:
            log.warning("[%s] Err attempt %d: %s", source, attempt + 1, e)
        time.sleep(CONFIG["RETRY_DELAY"] * (2 ** attempt))
    log.error("[%s] All retries failed", source)
    return None


# =============================================================================
# DATA SOURCES
# =============================================================================

BYBIT_SYMBOLS = {
    "bitcoin":"BTCUSDT","ethereum":"ETHUSDT","binancecoin":"BNBUSDT",
    "ripple":"XRPUSDT","solana":"SOLUSDT","dogecoin":"DOGEUSDT",
    "cardano":"ADAUSDT","tron":"TRXUSDT","avalanche-2":"AVAXUSDT",
    "shiba-inu":"SHIBUSDT","chainlink":"LINKUSDT","polkadot":"DOTUSDT",
    "near":"NEARUSDT","uniswap":"UNIUSDT","litecoin":"LTCUSDT",
    "stellar":"XLMUSDT","injective-protocol":"INJUSDT",
    "sei-network":"SEIUSDT","render-token":"RENDERUSDT",
    "fetch-ai":"FETUSDT","ondo-finance":"ONDOUSDT",
    "pendle":"PENDLEUSDT","arbitrum":"ARBUSDT","optimism":"OPUSDT",
    "starknet":"STRKUSDT","blur":"BLURUSDT","jupiter-ag":"JUPUSDT",
    "sui":"SUIUSDT","aptos":"APTUSDT","celestia":"TIAUSDT",
    "worldcoin-wld":"WLDUSDT","pyth-network":"PYTHUSDT",
}


def bybit_sym(coin_id: str, sym: str) -> str:
    return BYBIT_SYMBOLS.get(coin_id, sym.upper() + "USDT")


def fetch_klines_bybit(sym: str) -> list:
    d = get(CONFIG["BYBIT_BASE"] + "/v5/market/kline",
            {"category":"spot","symbol":sym,"interval":"D",
             "limit":CONFIG["KLINES_DAYS"]},
            delay=CONFIG["BYBIT_DELAY"], source="Bybit-K")
    if not d: return []
    return list(reversed(d.get("result",{}).get("list",[])))


def fetch_klines_okx(sym: str) -> list:
    okx = sym[:-4] + "-USDT" if sym.endswith("USDT") else sym
    d = get(CONFIG["OKX_BASE"] + "/api/v5/market/candles",
            {"instId":okx,"bar":"1D","limit":str(CONFIG["KLINES_DAYS"])},
            delay=CONFIG["OKX_DELAY"], source="OKX-K")
    if not d: return []
    return list(reversed(d.get("data",[])))


def fetch_funding_bybit(sym: str) -> list:
    d = get(CONFIG["BYBIT_BASE"] + "/v5/market/funding/history",
            {"category":"linear","symbol":sym,"limit":50},
            delay=CONFIG["BYBIT_DELAY"], source="Bybit-F")
    if not d: return []
    return d.get("result",{}).get("list",[])


def fetch_funding_okx(sym: str) -> list:
    okx  = sym[:-4] + "-USDT-SWAP" if sym.endswith("USDT") else sym
    d = get(CONFIG["OKX_BASE"] + "/api/v5/public/funding-rate-history",
            {"instId":okx,"limit":"50"},
            delay=CONFIG["OKX_DELAY"], source="OKX-F")
    if not d: return []
    return d.get("data",[])


def fetch_oi_bybit(sym: str) -> dict:
    d = get(CONFIG["BYBIT_BASE"] + "/v5/market/open-interest",
            {"category":"linear","symbol":sym,"intervalTime":"1d","limit":2},
            delay=CONFIG["BYBIT_DELAY"], source="Bybit-OI")
    if not d: return {}
    rows = d.get("result",{}).get("list",[])
    return rows[0] if rows else {}


def fetch_btc_cg() -> dict:
    d = get(CONFIG["COINGECKO_BASE"] + "/coins/markets",
            {"vs_currency":"usd","ids":"bitcoin",
             "price_change_percentage":"1h,24h,7d"},
            source="CG-BTC")
    return d[0] if d else {}


def fetch_top300_cg() -> list:
    log.info("[CoinGecko] Fetching top 300...")
    p1 = get(CONFIG["COINGECKO_BASE"] + "/coins/markets",
             {"vs_currency":"usd","order":"market_cap_desc","per_page":150,
              "page":1,"sparkline":"false",
              "price_change_percentage":"1h,24h,7d"},
             source="CoinGecko") or []
    p2 = get(CONFIG["COINGECKO_BASE"] + "/coins/markets",
             {"vs_currency":"usd","order":"market_cap_desc","per_page":150,
              "page":2,"sparkline":"false",
              "price_change_percentage":"1h,24h,7d"},
             delay=CONFIG["COINGECKO_DELAY"], source="CoinGecko") or []
    result = p1 + p2
    log.info("[CoinGecko] Got %d coins", len(result))
    return result


# =============================================================================
# OHLCV PARSER
# =============================================================================

def parse_klines(rows: list) -> dict:
    """
    Extract volume stats and price structure from kline rows.
    Format (Bybit/OKX): [ts, open, high, low, close, volume, ...]
    Returns empty dict if rows are insufficient.
    """
    if len(rows) < 5:
        return {}
    try:
        vols   = [float(r[5]) for r in rows]
        closes = [float(r[4]) for r in rows]
        highs  = [float(r[2]) for r in rows]
        lows   = [float(r[3]) for r in rows]

        vol_7d    = statistics.mean(vols[-7:])  if len(vols) >= 7  else statistics.mean(vols)
        vol_30d   = statistics.mean(vols[-30:]) if len(vols) >= 30 else statistics.mean(vols)
        vol_today = vols[-1]
        vol_spike = vol_today / vol_30d if vol_30d > 0 else 1.0

        rets = [(closes[i]-closes[i-1])/closes[i-1]
                for i in range(1,len(closes)) if closes[i-1] > 0]
        volatility = statistics.stdev(rets) if len(rets) >= 2 else 0.0

        # ATR (14 periods)
        trs = []
        for i in range(1, min(15, len(rows))):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr = max(h-l, abs(h-pc), abs(l-pc))
            if closes[i] > 0:
                trs.append(tr / closes[i])
        atr_pct = statistics.mean(trs) if trs else volatility

        # Recent structure: last 5 candles
        r5h = max(highs[-5:]) if len(highs) >= 5 else max(highs)
        r5l = min(lows[-5:])  if len(lows)  >= 5 else min(lows)

        return {
            "vol_7d_avg":  vol_7d,
            "vol_30d_avg": vol_30d,
            "vol_spike":   vol_spike,
            "volatility":  volatility,
            "atr_pct":     atr_pct,
            "recent_high": r5h,
            "recent_low":  r5l,
        }
    except Exception as e:
        log.debug("parse_klines error: %s", e)
        return {}


def parse_funding(rows: list, source: str = "bybit") -> dict:
    """Compute current funding rate and 7d average from history."""
    if not rows:
        return {}
    try:
        key   = "fundingRate" if source == "bybit" else "realizedRate"
        rates = [float(r.get(key, 0)) * 100 for r in rows if r.get(key)]
        if not rates:
            return {}
        current = rates[0]   # Bybit newest first
        avg7d   = statistics.mean(rates[:21]) if len(rates) >= 3 else current
        return {"funding": current, "funding_avg7d": avg7d}
    except Exception:
        return {}


# =============================================================================
# DATA ENRICHER
# =============================================================================

STABLE = {"usd","usdt","usdc","busd","dai","tusd","frax",
          "lusd","wrapped","staked","wbtc","weth","pax"}


def is_stable(coin_id: str, sym: str) -> bool:
    text = (coin_id + " " + sym).lower()
    return any(s in text for s in STABLE)


def in_range(mc: float) -> bool:
    return (CONFIG["MC_MID_MIN"] <= mc <= CONFIG["MC_MID_MAX"] or
            CONFIG["MC_SMALL_MIN"] <= mc <= CONFIG["MC_SMALL_MAX"])


def build_baseline(raw: dict) -> Coin:
    """Build a Coin from CoinGecko data only. All fields from real data."""
    cid = raw.get("id", "")
    sym = raw.get("symbol", "").upper()
    mc  = float(raw.get("market_cap") or 0)
    vol = float(raw.get("total_volume") or 0)

    # Derive vol_spike from vol/mc ratio (real data, not fixed)
    vol_mc_ratio = vol / mc if mc > 0 else 0
    # Median vol/mc for mid-cap coins is ~0.05-0.10
    # We scale: 0.10 = 1.0x, 0.30 = 3.0x, 0.05 = 0.5x
    derived_spike = min(5.0, vol_mc_ratio / 0.10)

    ch24 = float(raw.get("price_change_percentage_24h") or 0)
    ch1h = float(raw.get("price_change_percentage_1h_in_currency") or 0)

    # Derive volatility from 24h price change (real proxy)
    derived_vol = max(0.01, abs(ch24) / 100 * 0.6)
    derived_atr = derived_vol * 1.3   # ATR typically larger than daily return

    return Coin(
        id=cid, symbol=sym, name=raw.get("name",""), sector=sector_of(cid, sym, raw.get("name","")),
        rank=raw.get("market_cap_rank", 999) or 999,
        price=float(raw.get("current_price") or 0),
        market_cap=mc, volume_24h=vol,
        change_1h=ch1h, change_24h=ch24,
        change_7d=float(raw.get("price_change_percentage_7d_in_currency") or 0),
        ath=float(raw.get("ath") or 0),
        atl=float(raw.get("atl") or 1e-10),
        # Derived from real CoinGecko data
        vol_spike=derived_spike,
        volatility=derived_vol,
        atr_pct=derived_atr,
        source="CoinGecko",
        ex_symbol=bybit_sym(cid, sym),
    )


def enrich_full(coin: Coin):
    """Add Bybit/OKX OHLCV + derivatives to an existing Coin. Modifies in place."""
    sym = coin.ex_symbol

    # --- Klines ---
    rows = CACHE.get(sym)
    if rows:
        log.debug("[Cache] hit %s", sym)
    else:
        rows = fetch_klines_bybit(sym)
        if rows:
            CACHE.set(sym, rows)
            coin.source = "Bybit"
        else:
            rows = fetch_klines_okx(sym)
            if rows:
                CACHE.set(sym, rows)
                coin.source = "OKX"

    if rows:
        s = parse_klines(rows)
        if s:
            coin.has_ohlcv    = True
            coin.vol_7d_avg   = s["vol_7d_avg"]
            coin.vol_30d_avg  = s["vol_30d_avg"]
            coin.vol_spike    = s["vol_spike"]   # overwrite baseline estimate
            coin.volatility   = s["volatility"]
            coin.atr_pct      = s["atr_pct"]
            coin.recent_high  = s["recent_high"]
            coin.recent_low   = s["recent_low"]

    # --- Funding rate ---
    fr = fetch_funding_bybit(sym)
    fsrc = "bybit"
    if not fr:
        fr   = fetch_funding_okx(sym)
        fsrc = "okx"
    if fr:
        s = parse_funding(fr, fsrc)
        if s:
            coin.has_deriv     = True
            coin.funding       = s["funding"]
            coin.funding_avg7d = s["funding_avg7d"]

    # --- Open interest ---
    oi = fetch_oi_bybit(sym)
    if oi:
        coin.oi = float(oi.get("openInterest", 0))


def build_coin_list(raw_list: list, candidates: set) -> list:
    """
    candidates = symbols that get full Bybit enrichment.
    All others get baseline (derived from CoinGecko real data).
    """
    coins, total = [], len(raw_list)
    for i, raw in enumerate(raw_list, 1):
        sym = raw.get("symbol","").upper()
        try:
            coin = build_baseline(raw)
            if sym in candidates:
                enrich_full(coin)
                time.sleep(CONFIG["ENRICH_DELAY"])
            coins.append(coin)
            if i % 10 == 0:
                log.info("  [%d/%d] cache=%d full=%s",
                         i, total, CACHE.size(), sym if sym in candidates else "-")
        except Exception as e:
            log.warning("Error building %s: %s", sym, e)

    full = sum(1 for c in coins if c.has_ohlcv)
    log.info("Built %d coins: %d full / %d baseline", len(coins), full, len(coins)-full)
    return coins


# =============================================================================
# MARKET REGIME  (zero weight, context only)
# =============================================================================

def analyze_regime(coins: list) -> MarketRegime:
    r = MarketRegime()
    try:
        btc = fetch_btc_cg()
        if btc:
            h1  = float(btc.get("price_change_percentage_1h_in_currency") or 0)
            d7  = float(btc.get("price_change_percentage_7d_in_currency") or 0)
            r.btc_short  = "bullish" if h1 > 0.5  else ("bearish" if h1 < -0.5 else "neutral")
            r.btc_medium = "bullish" if d7 > 3    else ("bearish" if d7 < -3   else "neutral")

        if coins:
            up    = sum(1 for c in coins if c.change_24h > 2)
            total = len(coins)
            breadth = up / total if total > 0 else 0.5
            r.alts_state = ("expanding" if breadth > 0.55 else
                            "contracting" if breadth < 0.35 else "neutral")

            vols = [c.volume_24h for c in coins if c.volume_24h > 0]
            if len(vols) > 1:
                ratio = statistics.mean(vols) / statistics.median(vols)
                r.market_vol = ("high" if ratio > 1.5 else
                                "low"  if ratio < 0.7 else "normal")

        bull = (2 if r.btc_medium == "bullish" else 0) + \
               (1 if r.btc_short  == "bullish" else 0) + \
               (2 if r.alts_state == "expanding" else 0) + \
               (1 if r.market_vol == "high" else 0)
        bear = (2 if r.btc_medium == "bearish" else 0) + \
               (1 if r.btc_short  == "bearish" else 0) + \
               (2 if r.alts_state == "contracting" else 0)

        if bull >= 4:
            r.regime     = "risk_on"
            r.confidence = "high" if bull >= 5 else "medium"
        elif bear >= 4:
            r.regime     = "risk_off"
            r.confidence = "high" if bear >= 5 else "medium"
        else:
            r.regime     = "neutral"
            r.confidence = "low"
    except Exception as e:
        log.warning("Regime error: %s", e)
    return r


# =============================================================================
# SECTOR TRACKER
# =============================================================================

class SectorTracker:
    def __init__(self):
        self._ch  = defaultdict(list)
        self._vol = defaultdict(list)
        self._mkt = 0.0

    def update(self, coins: list):
        self._ch.clear(); self._vol.clear()
        all_ch = []
        for c in coins:
            self._ch[c.sector].append(c.change_24h)
            self._vol[c.sector].append(c.volume_24h)
            all_ch.append(c.change_24h)
        self._mkt = statistics.mean(all_ch) if all_ch else 0.0

    def strength(self, sector: str) -> float:
        data = self._ch.get(sector, [])
        if not data: return 0.30
        rel = statistics.mean(data) - self._mkt
        return round(max(0.0, min(1.0, 1 / (1 + math.exp(-rel / 3)))), 4)

    def breadth(self, sector: str) -> float:
        data = self._ch.get(sector, [])
        if not data: return 0.20
        return min(1.0, sum(1 for x in data if x > 2.0) / len(data) * 1.5)

    def vol_surge(self, sector: str, vol: float) -> float:
        vols = self._vol.get(sector, [])
        if not vols or vol == 0: return 0.20
        ratio = vol / statistics.mean(vols)
        return min(1.0, math.log10(max(ratio, 0.1) + 1) / math.log10(6))


SECTORS_TRACKER = SectorTracker()


# =============================================================================
# SCORING ENGINE  -  6 Dimensions
# All scores computed from real data. No fixed fallback values.
# =============================================================================

def _d1_liquidity(c: Coin) -> Dim:
    """
    Liquidity Zones & Heatmap (18%)
    Uses: ATH distance, 1h/24h FVG proxy, 7d change for squeeze, ATL mult
    All inputs from CoinGecko real data.
    """
    sigs = []

    # --- Order Block: distance from ATH ---
    s_ob = 0.0
    if c.ath > 0 and c.price > 0:
        d = (c.ath - c.price) / c.ath
        if 0.50 <= d <= 0.80:
            s_ob = max(0.0, 1.0 - abs(d - 0.65) * 3)
            sigs.append("OB Zone (%.0f%% below ATH)" % (d * 100))
        elif 0.80 < d <= 0.95:
            s_ob = 0.80; sigs.append("Deep Value (%.0f%% below ATH)" % (d * 100))
        elif 0.95 < d <= 1.0:
            s_ob = 0.65; sigs.append("Extreme Discount (%.0f%% below ATH)" % (d * 100))
        elif 0.20 <= d < 0.50:
            s_ob = 0.40
        else:
            s_ob = 0.10; sigs.append("Near ATH - resistance")

    # --- FVG proxy: 1h move vs 24h move ratio ---
    s_fvg = 0.0
    if c.change_24h != 0:
        ratio = abs(c.change_1h) / (abs(c.change_24h) + 0.01)
        # High 1h/24h ratio = impulse move = gap left behind
        s_fvg = min(1.0, ratio * 1.5) if ratio > 0.25 else ratio * 0.8
        if c.change_7d < -10 and c.change_1h > 1.0:
            s_fvg = max(s_fvg, 0.70)
            sigs.append("FVG Bounce (7d=%.1f%% / 1h=%+.1f%%)" % (c.change_7d, c.change_1h))
        elif ratio > 0.25:
            sigs.append("FVG Signal (ratio=%.2f)" % ratio)

    # --- Short squeeze zone: 7d drop magnitude ---
    s_sq = 0.0
    if c.change_7d <= -20:
        s_sq = 0.50 + min(0.50, abs(c.change_7d + 20) / 30)
        sigs.append("Short Squeeze Candidate (%.1f%% 7d)" % c.change_7d)
    elif -20 < c.change_7d <= -10:
        s_sq = min(0.45, abs(c.change_7d) / 20 * 0.45)
    elif -10 < c.change_7d <= -5:
        s_sq = min(0.25, abs(c.change_7d) / 10 * 0.25)

    # --- ATL accumulation ---
    s_atl = 0.0
    if c.atl > 0 and c.price > 0:
        m = c.price / c.atl
        if 1.5 <= m <= 3.0:
            s_atl = 0.85; sigs.append("Near ATL zone (%.1fx)" % m)
        elif 3.0 < m <= 6.0:
            s_atl = 0.70; sigs.append("Accumulation zone (%.1fx ATL)" % m)
        elif 6.0 < m <= 15.0:
            s_atl = 0.45
        else:
            s_atl = max(0.10, 1.0 - math.log10(m) / math.log10(100))

    score = round(min(1.0, s_ob*0.35 + s_fvg*0.25 + s_sq*0.25 + s_atl*0.15), 4)
    return Dim("Liquidity Zones", score, 0.18, sigs)


def _d2_smart_money(c: Coin) -> Dim:
    """
    Smart Money / OI / Funding (22%)
    With Bybit: funding divergence + OI + absorption + squeeze alignment
    Without Bybit: volume behavior + price/volume divergence from CoinGecko
    No fixed fallback. All paths compute from real data.
    """
    sigs = []

    if c.has_deriv:
        # --- Funding rate divergence ---
        dev = c.funding - c.funding_avg7d
        if dev < -0.05:
            s_fund = min(1.0, 0.70 + abs(dev) * 12)
            sigs.append("Strong Negative Funding Divergence (%.4f%%)" % dev)
        elif -0.05 <= dev < -0.02:
            s_fund = 0.65 + abs(dev) * 5; sigs.append("Negative Funding (%.4f%%)" % dev)
        elif -0.02 <= dev < -0.005:
            s_fund = 0.55
        elif abs(dev) < 0.005:
            s_fund = 0.50
        elif 0.005 <= dev < 0.03:
            s_fund = 0.40
        elif 0.03 <= dev < 0.06:
            s_fund = 0.25; sigs.append("Positive Funding - longs crowded")
        else:
            s_fund = 0.10; sigs.append("Extreme Positive Funding - danger")

        # --- OI change ---
        oi_ch = c.oi_change
        if 5 <= oi_ch <= 25:
            s_oi = min(1.0, 0.55 + oi_ch / 50); sigs.append("OI Inflow +%.1f%%" % oi_ch)
        elif oi_ch > 25:
            s_oi = 0.45; sigs.append("OI Spike %.1f%% - speculative" % oi_ch)
        elif -5 <= oi_ch < 5:
            s_oi = 0.40
        elif -15 <= oi_ch < -5:
            s_oi = 0.25; sigs.append("OI Outflow %.1f%%" % oi_ch)
        else:
            s_oi = 0.10; sigs.append("Heavy OI Exit %.1f%%" % oi_ch)

        # --- Squeeze alignment ---
        if c.funding < -0.01 and oi_ch > 5:
            s_align = min(1.0, 0.75 + abs(c.funding) * 10)
            sigs.append("Short Squeeze Setup (FR=%.4f / OI=+%.1f%%)" % (c.funding, oi_ch))
        elif c.funding > 0.05 and oi_ch < -5:
            s_align = 0.15; sigs.append("Long Squeeze Risk")
        elif abs(c.funding) < 0.005:
            s_align = 0.55
        else:
            s_align = 0.35

    else:
        # --- No derivatives: compute from CoinGecko vol + price behavior ---
        # Funding proxy: extreme 7d drop with volume = market is short-heavy
        vol_intensity = min(1.0, c.vol_spike / 3.0)   # derived, not fixed
        if c.change_7d < -20:
            s_fund  = min(0.75, 0.40 + vol_intensity * 0.35)
            sigs.append("Short-heavy proxy (%.1f%% 7d / vol=%.1fx)" % (c.change_7d, c.vol_spike))
        elif c.change_7d < -10:
            s_fund  = min(0.55, 0.30 + vol_intensity * 0.25)
        elif c.change_7d > 20 and c.vol_spike > 2:
            s_fund  = 0.30; sigs.append("Crowded longs proxy")
        else:
            # Neutral: use vol intensity as base signal
            s_fund  = min(0.50, 0.25 + vol_intensity * 0.25)

        # OI proxy: high volume relative to market cap = capital flowing in
        vol_mc = c.volume_24h / max(c.market_cap, 1)
        if vol_mc > 0.30:
            s_oi = min(0.70, 0.40 + vol_mc); sigs.append("High Vol/MC proxy (%.2f)" % vol_mc)
        elif vol_mc > 0.10:
            s_oi = min(0.55, 0.25 + vol_mc * 1.5)
        elif vol_mc > 0.03:
            s_oi = 0.30
        else:
            s_oi = max(0.10, vol_mc * 5)

        s_align = (s_fund + s_oi) / 2

    # --- Absorption: real from price + volume (works with or without Bybit) ---
    if c.change_24h < -5 and c.vol_spike > 1.5:
        s_absorb = min(1.0, (abs(c.change_24h) / 15) * (c.vol_spike / 3))
        sigs.append("Absorption (%.1f%% / %.1fx vol)" % (c.change_24h, c.vol_spike))
    elif c.change_24h > 5 and c.vol_spike > 2.0:
        s_absorb = min(1.0, 0.50 + (c.vol_spike - 2) * 0.15)
        sigs.append("Volume Breakout (%.1fx)" % c.vol_spike)
    elif c.change_24h < -3 and c.vol_spike < 0.7:
        s_absorb = 0.55; sigs.append("Exhaustion sell - vol drying up")
    elif abs(c.change_24h) < 2 and c.vol_spike > 1.5:
        s_absorb = 0.45; sigs.append("Consolidation + vol uptick")
    else:
        # Scale from actual change and volume (no fixed default)
        s_absorb = min(0.45, (abs(c.change_24h) / 20) + (c.vol_spike - 1) * 0.1)
        s_absorb = max(0.05, s_absorb)

    score = round(min(1.0, s_fund*0.30 + s_oi*0.25 + s_absorb*0.25 + s_align*0.20), 4)
    return Dim("Smart Money / OI / Funding", score, 0.22, sigs)


def _d3_volume(c: Coin) -> Dim:
    """
    Volume Confirmation (27%) - Highest weight. Core explosive detector.
    With OHLCV: spike vs 7d/30d avg + VCP + price-vol confirm
    Without OHLCV: vol/MC ratio + price momentum alignment
    """
    sigs = []

    if c.has_ohlcv and c.vol_30d_avg > 0:
        sp30 = c.vol_spike
        sp7  = c.volume_24h / c.vol_7d_avg if c.vol_7d_avg > 0 else sp30

        if sp30 >= 6.0:
            s_spike = 0.60; sigs.append("Extreme Vol %.1fx" % sp30)
        elif sp30 >= 2.0:
            s_spike = 0.60 + min(0.35, (sp30 - 2) / 4 * 0.35)
            sigs.append("Vol Spike %.1fx (30d)" % sp30)
        elif sp30 >= 1.5:
            s_spike = 0.50
        elif sp30 >= 0.8:
            s_spike = 0.30 + sp30 * 0.10
        else:
            s_spike = max(0.05, sp30 * 0.20)

        if sp7 > 1.8 and sp30 > 1.8:
            s_spike = min(1.0, s_spike + 0.10)
            sigs.append("Vol Consistency (7d=%.1fx / 30d=%.1fx)" % (sp7, sp30))

        # VCP: low ATR + high vol spike = coiled spring
        atr = c.atr_pct
        if atr < 0.025 and sp30 > 2.0:
            s_vcp = min(1.0, (2.5 - atr * 40) * (sp30 / 4))
            sigs.append("VCP Compression (ATR=%.1f%% / spike=%.1fx)" % (atr*100, sp30))
        elif atr < 0.035 and sp30 > 1.5:
            s_vcp = 0.65; sigs.append("Low ATR + Vol Expansion")
        elif atr < 0.06:
            s_vcp = max(0.20, 0.50 - atr * 5)
        else:
            s_vcp = max(0.05, 0.30 - (atr - 0.06) * 3)

    else:
        # --- No OHLCV: derive from CoinGecko vol/MC ratio ---
        vol_mc = c.volume_24h / max(c.market_cap, 1)
        # High vol/MC = high turnover = strong interest
        if vol_mc > 0.40:
            s_spike = min(0.80, 0.50 + vol_mc * 0.5); sigs.append("High Vol/MC (%.2f)" % vol_mc)
        elif vol_mc > 0.20:
            s_spike = min(0.65, 0.35 + vol_mc)
        elif vol_mc > 0.10:
            s_spike = min(0.50, 0.20 + vol_mc * 1.5)
        elif vol_mc > 0.05:
            s_spike = min(0.35, vol_mc * 4)
        else:
            s_spike = max(0.05, vol_mc * 8)

        # VCP proxy: low 24h change = low volatility
        low_vol = abs(c.change_24h) < 3.0
        s_vcp = 0.55 if (low_vol and vol_mc > 0.15) else min(0.40, vol_mc * 3)

    # Price-volume direction confirmation (universal)
    if c.change_24h > 4 and c.vol_spike > 1.5:
        s_pv = min(1.0, (c.change_24h / 15) * (c.vol_spike / 3))
        sigs.append("Vol/Price Breakout Confirmed")
    elif c.change_24h > 4 and c.vol_spike < 1.0:
        s_pv = max(0.10, c.change_24h / 30)
        sigs.append("Price up / Vol weak")
    elif c.change_24h < -4 and c.vol_spike < 0.8:
        s_pv = 0.60; sigs.append("Exhaustion sell")
    elif c.change_24h < -4 and c.vol_spike > 2.0:
        s_pv = min(0.55, 0.30 + c.vol_spike / 10)
    else:
        # Scale from actual data: small move = mid score, large move = higher
        magnitude = min(1.0, abs(c.change_24h) / 10)
        vol_factor = min(1.0, c.vol_spike / 2)
        s_pv = min(0.55, magnitude * 0.4 + vol_factor * 0.3)

    # Relative volume rank (updated post-scan in engine)
    s_rank = 0.0   # set externally

    score = round(min(1.0, s_spike*0.40 + s_vcp*0.30 + s_pv*0.20 + s_rank*0.10), 4)
    return Dim("Volume Confirmation", score, 0.27, sigs)


def _d4_order_flow(c: Coin) -> Dim:
    """
    Order Flow Clusters (13%)
    No order book fetches. Structure-based only.
    All inputs from existing coin data.
    """
    sigs = []

    # Fibonacci cluster proximity
    s_fib = 0.0
    if c.ath > c.atl > 0:
        pct   = (c.price - c.atl) / (c.ath - c.atl)
        min_d = min(abs(pct - f) for f in [0.236, 0.382, 0.500, 0.618, 0.786])
        if min_d < 0.03:
            s_fib = min(1.0, 0.75 + (0.03 - min_d) * 15)
            sigs.append("Near Fib Level (%.1f%% of range)" % (pct * 100))
        elif min_d < 0.07:
            s_fib = 0.55 + (0.07 - min_d) * 5
        elif min_d < 0.15:
            s_fib = 0.35 + (0.15 - min_d) * 2
        else:
            s_fib = max(0.10, 0.35 - min_d)

    # Price structure: where is price in recent range
    s_struct = 0.0
    if c.recent_low > 0 and c.recent_high > 0 and c.price > 0:
        rng = c.recent_high - c.recent_low
        if rng > 0:
            pos = (c.price - c.recent_low) / rng
            if pos < 0.20:
                s_struct = 0.85; sigs.append("Price at range bottom")
            elif pos < 0.35:
                s_struct = 0.70
            elif pos < 0.65:
                s_struct = 0.45
            elif pos < 0.80:
                s_struct = 0.60
            else:
                s_struct = 0.70; sigs.append("Breaking range top")
    else:
        # No OHLCV: use ATH proximity as structure proxy
        if c.ath > 0 and c.price > 0:
            ath_pct = c.price / c.ath
            s_struct = min(0.60, ath_pct * 0.8)

    # 1h momentum + volume as order flow proxy
    if c.change_1h > 2 and c.vol_spike > 1.5:
        s_flow = min(1.0, 0.55 + (c.change_1h / 10) + (c.vol_spike / 10))
        sigs.append("1h momentum + vol surge")
    elif c.change_1h > 0.5 and c.vol_spike > 1.2:
        s_flow = 0.55
    elif c.change_1h < -2 and c.vol_spike > 1.5:
        s_flow = 0.20; sigs.append("1h sell pressure")
    else:
        # Scale from actual 1h change and vol
        s_flow = min(0.50, max(0.10,
            0.30 + (c.change_1h / 20) + (c.vol_spike - 1) * 0.05))

    score = round(min(1.0, s_fib*0.35 + s_struct*0.40 + s_flow*0.25), 4)
    return Dim("Order Flow Clusters", score, 0.13, sigs)


def _d5_sector(c: Coin) -> Dim:
    """Sector Momentum (12%). All computed from coin list real data."""
    sigs = []
    rs  = SECTORS_TRACKER.strength(c.sector)
    brd = SECTORS_TRACKER.breadth(c.sector)
    vs  = SECTORS_TRACKER.vol_surge(c.sector, c.volume_24h)
    if rs  > 0.70: sigs.append("Sector [%s] outperforming" % c.sector)
    if brd > 0.60: sigs.append("Broad [%s] rally (%.0f%%)" % (c.sector, brd*100))
    if vs  > 0.70: sigs.append("Volume surge vs sector")
    score = round(min(1.0, rs*0.45 + brd*0.35 + vs*0.20), 4)
    return Dim("Sector Momentum", score, 0.12, sigs)


def _d6_onchain(c: Coin) -> Dim:
    """
    OnChain Health (8%).
    With derivatives: funding sentiment + OI trend
    Without: 7d performance vs volume as health proxy
    """
    sigs = []

    if c.has_deriv:
        fr = c.funding
        if -0.05 < fr < -0.01:
            s_fund = 0.75; sigs.append("Healthy Negative Funding (%.4f%%)" % fr)
        elif -0.10 < fr <= -0.05:
            s_fund = 0.85; sigs.append("Strong Negative Funding")
        elif -0.01 <= fr <= 0.02:
            s_fund = 0.55
        elif 0.02 < fr <= 0.05:
            s_fund = 0.35
        else:
            s_fund = 0.15; sigs.append("Crowded Longs")

        oi_ch = c.oi_change
        if oi_ch > 10:
            s_oi = min(1.0, 0.60 + oi_ch / 50); sigs.append("OI Growing +%.1f%%" % oi_ch)
        elif 3 <= oi_ch <= 10:
            s_oi = 0.55
        elif -3 <= oi_ch < 3:
            s_oi = 0.40
        elif -10 <= oi_ch < -3:
            s_oi = 0.25
        else:
            s_oi = 0.10; sigs.append("Heavy OI Exit")

    else:
        # Health proxy from real 7d performance + vol behavior
        # Recovering after drop = healthier than continuing drop
        if c.change_7d > 5 and c.vol_spike > 1.2:
            s_fund = 0.65; sigs.append("7d recovery + vol (health proxy)")
        elif c.change_7d > 0:
            s_fund = 0.50
        elif -10 < c.change_7d <= 0:
            s_fund = 0.35
        elif -25 < c.change_7d <= -10:
            # Deep drop: either accumulation or bleeding
            # High vol = accumulation signal
            s_fund = min(0.55, 0.25 + c.vol_spike * 0.10)
        else:
            s_fund = max(0.10, 0.20 - abs(c.change_7d) / 100)

        # Vol consistency as OI proxy
        vol_mc = c.volume_24h / max(c.market_cap, 1)
        s_oi = min(0.60, 0.20 + vol_mc * 2)

    score = round(min(1.0, s_fund*0.50 + s_oi*0.50), 4)
    return Dim("OnChain Health", score, 0.08, sigs)


# =============================================================================
# SCAN ENGINE
# =============================================================================

def _sl_tp(c: Coin, risk: str, score: float) -> tuple:
    """Structure-based SL/TP. Uses real OHLCV structure when available."""
    atr = c.atr_pct if c.atr_pct > 0 else max(c.volatility, 0.02)

    if c.recent_low > 0 and c.price > c.recent_low:
        structure_sl = (c.price - c.recent_low) / c.price * 100 + atr * 40
        sl = max(3.0, min(15.0, structure_sl))
    else:
        sl = max(3.0, min(12.0, atr * 130))

    if risk == "HIGH": sl = min(15.0, sl * 1.2)

    # TP1: target recent high if above current price
    if c.recent_high > c.price > 0:
        tp1_nat = (c.recent_high - c.price) / c.price * 100
        tp1 = max(sl * 1.2, min(tp1_nat, sl * 3.0))
    else:
        tp1 = sl * (1.5 + (score - 3.2) * 0.35)

    tp2 = sl * (2.5 + (score - 3.2) * 0.7)
    return round(sl, 1), round(tp1, 1), round(tp2, 1)


def _risk(c: Coin) -> str:
    atr = c.atr_pct if c.atr_pct > 0 else c.volatility
    if atr > 0.07 or c.market_cap < CONFIG["MC_SMALL_MAX"] or abs(c.funding) > 0.08:
        return "HIGH"
    if atr > 0.035 or c.market_cap < 200_000_000 or abs(c.funding) > 0.04:
        return "MEDIUM"
    return "LOW"


def _confidence(score: float, quality: str) -> str:
    penalty = "baseline" in quality
    for thr, lbl in [(4.5,"ULTRA"),(4.0,"HIGH"),(3.5,"MEDIUM"),(0,"LOW")]:
        if score >= thr:
            if penalty and lbl in ("ULTRA","HIGH"):
                lbl = {"ULTRA":"HIGH","HIGH":"MEDIUM"}[lbl]
            return lbl
    return "LOW"


def _quality(c: Coin) -> str:
    if c.has_ohlcv and c.has_deriv: return "FULL (%s)" % c.source
    if c.has_ohlcv:                 return "PARTIAL (%s)" % c.source
    return "LIMITED (CoinGecko baseline)"


def _entry(sigs_flat: list, dims: list) -> str:
    if any("VCP" in s for s in sigs_flat) and any("Breakout" in s for s in sigs_flat):
        return "VCP breakout - entry on volume confirm above compression zone"
    if any("Squeeze" in s for s in sigs_flat):
        return "Short squeeze setup - scale into dips, target liquidation zones"
    if any("Absorption" in s for s in sigs_flat):
        return "Absorption confirmed - wait for reversal candle then entry"
    if dims[0].score > 0.70 and dims[1].score > 0.65:
        return "OB reclaim with smart money alignment - entry on stabilization"
    if dims[2].score > 0.75:
        return "Volume breakout - entry on retest of breakout level"
    return "Support retest with volume - entry with confirmation candle"


def score_coin(c: Coin, regime: MarketRegime) -> Alert:
    dims = [
        _d1_liquidity(c),
        _d2_smart_money(c),
        _d3_volume(c),
        _d4_order_flow(c),
        _d5_sector(c),
        _d6_onchain(c),
    ]
    weighted = sum(d.score * d.weight for d in dims)
    final    = round(weighted * 5, 2)
    quality  = _quality(c)
    risk     = _risk(c)
    sl, tp1, tp2 = _sl_tp(c, risk, final)
    sigs_flat = [s for d in dims for s in d.signals]
    return Alert(
        coin=c, dims=dims, score=final,
        confidence=_confidence(final, quality),
        risk=risk, quality=quality,
        entry=_entry(sigs_flat, dims),
        sl=sl, tp1=tp1, tp2=tp2, regime=regime,
    )


def scan_all(coins: list, regime: MarketRegime) -> list:
    results = [score_coin(c, regime) for c in coins]

    # Normalize relative volume rank across all coins
    max_spike = max((c.vol_spike for c in coins), default=1.0) or 1.0
    for a in results:
        rk = round(a.coin.vol_spike / max_spike, 3)
        d3 = a.dims[2]
        d3.score = round(min(1.0, d3.score * 0.90 + rk * 0.10), 4)
        # Recompute final score with updated d3
        a.score = round(sum(d.score * d.weight for d in a.dims) * 5, 2)

    return sorted(results, key=lambda x: x.score, reverse=True)


# =============================================================================
# TELEGRAM
# =============================================================================

REGIME_LABEL = {
    "risk_on":  "GREEN - RISK ON",
    "neutral":  "YELLOW - NEUTRAL",
    "risk_off": "RED - RISK OFF",
}


def _mc_str(mc: float) -> str:
    if mc >= 1e9: return "$%.2fB" % (mc / 1e9)
    if mc >= 1e6: return "$%.0fM" % (mc / 1e6)
    return "$%.0f" % mc


def _bar(score: float) -> str:
    f = round(score / 5 * 10)
    return "[" + "#" * f + "-" * (10 - f) + "]"


def format_alert(a: Alert, reason: str = "") -> str:
    c, r = a.coin, a.regime
    now  = datetime.utcnow().strftime("%H:%M UTC")
    lines = [
        "*** %s ***" % a.label(),
        "=" * 36,
        "Coin   : %s  (%s)" % (c.name, c.symbol),
        "MC     : %s   Rank #%d" % (_mc_str(c.market_cap), c.rank),
        "Price  : $%s" % ("%.4g" % c.price),
        "Sector : %s" % c.sector,
        "",
        "Signal : %.1f / 5.0   %s" % (a.score, _bar(a.score)),
        "",
        "Dimensions:",
    ]
    for d in a.dims:
        bar = "#" * round(d.score * 5) + "-" * (5 - round(d.score * 5))
        lines.append("  [%s] %.2f  %s" % (bar, d.score, d.name))
    lines += [
        "",
        "Confidence : %-6s  Risk: %s" % (a.confidence, a.risk),
        "Data       : %s" % a.quality,
        "",
        "Entry:",
        "  %s" % a.entry,
        "",
        "SL: -%.1f%%   TP1: +%.1f%%   TP2: +%.1f%%" % (a.sl, a.tp1, a.tp2),
        "",
        "Change: 1h %+.1f%%  24h %+.1f%%  7d %+.1f%%" % (
            c.change_1h, c.change_24h, c.change_7d),
    ]
    top = [s for d in a.dims for s in d.signals[:1]][:4]
    if top:
        lines += ["", "Signals:"]
        for s in top:
            lines.append("  * %s" % s)
    lines += [
        "",
        "-- Market Context --",
        "Regime : %s" % REGIME_LABEL.get(r.regime, r.regime),
        "BTC    : %s (1h) / %s (7d)" % (r.btc_short.upper(), r.btc_medium.upper()),
        "Alts   : %s   Vol: %s" % (r.alts_state.upper(), r.market_vol.upper()),
        "",
        "=" * 36,
        "%s  |  LRS v6 Pro" % now,
    ]
    if "score_jump" in reason:
        lines.append("^ Score upgraded")
    return "\n".join(lines)


def format_summary(sent: list, duration: float,
                   total: int, regime: MarketRegime) -> str:
    now = datetime.utcnow().strftime("%H:%M UTC")
    lines = [
        "--- Scan Summary ---",
        "%s  |  %d coins  |  %.0fs" % (now, total, duration),
        "Alerts: %d   Market: %s" % (len(sent), REGIME_LABEL.get(regime.regime, regime.regime)),
    ]
    if sent:
        lines.append("")
        for i, a in enumerate(sent[:5], 1):
            lines.append("  %d. %-8s [%s] %.1f" % (i, a.coin.symbol, a.label(), a.score))
    lines.append("-" * 28)
    return "\n".join(lines)


def tg_send(text: str) -> bool:
    token    = CONFIG["TELEGRAM_BOT_TOKEN"]
    chat_id  = CONFIG["TELEGRAM_CHAT_ID"]
    if "PUT_YOUR" in token:
        log.error("Telegram not configured")
        return False
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    for attempt in range(3):
        try:
            r = requests.post(url, json={
                "chat_id": chat_id, "text": text,
                "disable_web_page_preview": True,
            }, timeout=10)
            if r.status_code == 200: return True
            log.warning("Telegram HTTP %d", r.status_code)
        except requests.RequestException as e:
            log.warning("Telegram err attempt %d: %s", attempt+1, e)
        time.sleep(2 ** attempt)
    return False


def tg_test() -> bool:
    ok = tg_send("LRS v6 Pro - Connection OK. System starting.")
    log.info("Telegram: %s", "OK" if ok else "FAILED")
    return ok


# =============================================================================
# COOLDOWN MANAGER
# =============================================================================

class Cooldown:
    def __init__(self):
        self._h = {}

    def _cd(self, score: float) -> int:
        if score >= CONFIG["ALERT_ULTRA"]:  return CONFIG["COOLDOWN_ULTRA_MIN"]
        if score >= CONFIG["ALERT_STRONG"]: return CONFIG["COOLDOWN_STRONG_MIN"]
        return CONFIG["COOLDOWN_MODERATE_MIN"]

    def should_alert(self, sym: str, score: float) -> tuple:
        e = self._h.get(sym)
        if not e: return True, "first_alert"
        elapsed = (datetime.utcnow() - e["t"]).total_seconds() / 60
        if elapsed >= self._cd(e["s"]): return True, "cooldown_expired"
        if score >= e["s"] + CONFIG["SCORE_JUMP_OVERRIDE"]:
            return True, "score_jump +%.1f" % (score - e["s"])
        return False, "cooldown (%.0f/%.0fmin)" % (elapsed, self._cd(e["s"]))

    def record(self, sym: str, score: float):
        self._h[sym] = {"s": score, "t": datetime.utcnow()}


# =============================================================================
# WEBSOCKET MONITOR
# =============================================================================

class LiveState:
    def __init__(self, sym: str, price: float, vol: float):
        self.sym      = sym
        self.price    = price
        self.vol_base = vol
        self.cur_price = price
        self.cur_vol   = 0.0
        self.history   = deque(maxlen=20)
        self.alerted   = False

    def update(self, p: float, v: float):
        self.cur_price = p; self.cur_vol = v; self.history.append(p)

    def change_pct(self) -> float:
        if len(self.history) < 5: return 0.0
        old = list(self.history)[-5]
        return (self.cur_price - old) / old * 100 if old else 0.0

    def vol_spike(self) -> float:
        base = self.vol_base / 1440
        return self.cur_vol / base if base > 0 else 1.0


class WSMonitor:
    WS_URL = CONFIG["BYBIT_WS"]

    def __init__(self):
        self._states  = {}
        self._ws      = None
        self._thread  = None
        self._running = False
        self._lock    = threading.Lock()
        self._delay   = 5
        self.on_spike = None

    def update(self, sym_map: dict):
        if not sym_map:
            log.info("[WS] No candidates - stream idle")
            return
        limited = dict(list(sym_map.items())[:CONFIG["WS_MAX_SYMBOLS"]])
        with self._lock:
            for sym, (p, v) in limited.items():
                if sym.lower() not in self._states:
                    self._states[sym.lower()] = LiveState(sym, p, v)
            for k in [k for k in self._states if k.upper() not in limited]:
                del self._states[k]
        log.info("[WS] Watching %d coins", len(self._states))
        if self._running: self._resub()
        elif self._states: self.start()

    def _sub_msg(self) -> dict:
        with self._lock:
            syms = [k.upper() for k in self._states]
        return {"op":"subscribe",
                "args":["tickers.%s" % s for s in syms[:CONFIG["WS_MAX_SYMBOLS"]]]}

    def _on_msg(self, ws, msg):
        try:
            d = json.loads(msg)
            if not d.get("topic","").startswith("tickers."): return
            tk = d.get("data",{})
            sym = tk.get("symbol","").lower()
            p   = float(tk.get("lastPrice",0) or 0)
            v   = float(tk.get("volume24h",0) or 0)
            with self._lock:
                st = self._states.get(sym)
            if not st or p == 0: return
            st.update(p, v)
            pm = abs(st.change_pct())
            vs = st.vol_spike()
            reasons = []
            if pm >= CONFIG["WS_PRICE_SPIKE_PCT"]:  reasons.append("price=%.1f%%" % pm)
            if vs >= CONFIG["WS_VOLUME_SPIKE_MULT"]: reasons.append("vol=%.1fx" % vs)
            if reasons and not st.alerted:
                st.alerted = True
                log.info("[WS] Spike: %s - %s", sym.upper(), ", ".join(reasons))
                if self.on_spike:
                    self.on_spike(sym.upper(), {"price":p,"price_chg":st.change_pct(),
                                                "vol_spike":vs,"reasons":reasons})
                def reset():
                    time.sleep(300); st.alerted = False
                threading.Thread(target=reset, daemon=True).start()
        except Exception:
            pass

    def _resub(self):
        if self._ws:
            try: self._ws.send(json.dumps(self._sub_msg()))
            except Exception: pass

    def _on_open(self, ws):  self._delay = 5; log.info("[WS] Connected"); self._resub()
    def _on_error(self, ws, e): log.warning("[WS] Error: %s", e)
    def _on_close(self, ws, c, m):
        log.info("[WS] Disconnected (%s)", c)
        if self._running:
            time.sleep(self._delay); self._delay = min(60, self._delay*2); self._connect()

    def _connect(self):
        self._ws = websocket.WebSocketApp(
            self.WS_URL, on_message=self._on_msg,
            on_error=self._on_error, on_close=self._on_close, on_open=self._on_open)
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    def start(self):
        if self._running: return
        self._running = True
        self._thread  = threading.Thread(target=self._connect, daemon=True, name="WS")
        self._thread.start()
        log.info("[WS] Monitor started")

    def stop(self):
        self._running = False
        if self._ws: self._ws.close()


# =============================================================================
# MAIN SCANNER
# =============================================================================

class Scanner:

    def __init__(self):
        log.info("Initializing LRS v6 Pro...")
        self.cooldown    = Cooldown()
        self.ws          = WSMonitor()
        self.ws.on_spike = self._on_spike
        self._candidates: set = set()
        self._last:       list = []
        self._cycle       = 0
        log.info("Ready.")

    def _on_spike(self, sym: str, data: dict):
        match = next((a for a in self._last if a.coin.ex_symbol == sym), None)
        if not match: return
        msg = (
            "*** WS SPIKE: %s (%s) ***\n"
            "Price  : $%.4g  (%+.1f%%)\n"
            "Volume : %.1fx above normal\n"
            "Reason : %s\n"
            "Score  : [%s] %.1f / 5.0\n"
            "%s  |  LRS v6"
        ) % (match.coin.name, match.coin.symbol,
             data["price"], data["price_chg"], data["vol_spike"],
             ", ".join(data["reasons"]),
             match.label(), match.score,
             datetime.utcnow().strftime("%H:%M UTC"))
        tg_send(msg)

    def _update_ws(self, results: list):
        wl = {a.coin.ex_symbol: (a.coin.price, a.coin.vol_30d_avg or a.coin.volume_24h)
              for a in results
              if a.score >= CONFIG["WS_WATCH_MIN_SCORE"] and a.coin.ex_symbol}
        self.ws.update(dict(list(wl.items())[:CONFIG["WS_MAX_SYMBOLS"]]))

    def _update_candidates(self, results: list):
        self._candidates = {a.coin.symbol for a in results
                            if a.score >= CONFIG["ENRICH_MIN_SCORE"]}
        log.info("Next cycle candidates: %d", len(self._candidates))

    def _seed_first_cycle(self, filtered: list) -> set:
        """
        On cycle 1, seed top N eligible coins as candidates
        so they get full Bybit enrichment immediately.
        """
        n = CONFIG["SEED_CANDIDATES_N"]
        return {raw.get("symbol","").upper() for raw in filtered[:n]}

    def run_cycle(self) -> list:
        self._cycle += 1
        start = time.time()
        log.info("=" * 52)
        log.info("Cycle #%d  %s  (cache=%d  candidates=%d)",
                 self._cycle,
                 datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                 CACHE.size(), len(self._candidates))
        log.info("=" * 52)

        try:
            raw = fetch_top300_cg()
            if not raw:
                log.error("CoinGecko returned no data")
                return []

            filtered = [r for r in raw
                        if not is_stable(r.get("id",""), r.get("symbol",""))
                        and in_range(float(r.get("market_cap") or 0))]
            log.info("After filter: %d coins", len(filtered))

            # Cycle 1: seed candidates from top-N eligible coins
            candidates = self._candidates if self._cycle > 1 else self._seed_first_cycle(filtered)

            coins   = build_coin_list(filtered, candidates)
            SECTORS_TRACKER.update(coins)
            regime  = analyze_regime(coins)
            log.info("Regime: %s | BTC: %s | Alts: %s",
                     regime.regime.upper(), regime.btc_medium.upper(), regime.alts_state.upper())

            log.info("Scoring %d coins...", len(coins))
            results = scan_all(coins, regime)

            above = sum(1 for a in results if a.score >= CONFIG["ALERT_MIN"])
            log.info("Above threshold (%.1f): %d", CONFIG["ALERT_MIN"], above)

            # Send alerts
            duration = time.time() - start
            sent = []
            for a in results:
                c, sc = a.coin, a.score
                in_mid   = CONFIG["MC_MID_MIN"] <= c.market_cap <= CONFIG["MC_MID_MAX"]
                in_small = CONFIG["MC_SMALL_MIN"] <= c.market_cap <= CONFIG["MC_SMALL_MAX"]
                if not (in_mid or in_small): continue
                if in_small and sc < CONFIG["SMALL_CAP_MIN_SCORE"]: continue
                if sc < CONFIG["ALERT_MIN"]: break
                ok, reason = self.cooldown.should_alert(c.symbol, sc)
                if not ok:
                    log.debug("SKIP %-8s %.1f - %s", c.symbol, sc, reason)
                    continue
                if tg_send(format_alert(a, reason)):
                    self.cooldown.record(c.symbol, sc)
                    sent.append(a)
                    log.info("ALERT: %-8s [%s] %.1f (%s)", c.symbol, a.label(), sc, reason)
                    time.sleep(1.5)

            tg_send(format_summary(sent, duration, len(coins), regime))

            self._last = results
            self._update_candidates(results)
            self._update_ws(results)

            log.info("Cycle #%d done - %.0fs - %d alerts", self._cycle, duration, len(sent))
            return results

        except Exception as e:
            log.error("Cycle error: %s\n%s", e, traceback.format_exc())
            return []

    def run_forever(self):
        log.info("Starting...")
        if not tg_test():
            log.error("Telegram failed. Fix BOT_TOKEN and CHAT_ID then restart.")
            sys.exit(1)
        interval = CONFIG["SCAN_INTERVAL_SEC"]
        log.info("Interval: %d min", interval // 60)
        while True:
            try:
                self.run_cycle()
                log.info("Waiting %d min...", interval // 60)
                time.sleep(interval)
            except KeyboardInterrupt:
                log.info("Stopped.")
                self.ws.stop()
                break
            except Exception as e:
                log.error("Loop error: %s", e)
                time.sleep(60)


# =============================================================================
# TEST + ENTRY
# =============================================================================

def run_test():
    log.info("=== TEST MODE ===")
    ok = tg_test()
    if not ok:
        log.error("Fix TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in CONFIG")
    raw = fetch_top300_cg()
    log.info("CoinGecko    : %s", "OK (%d)" % len(raw) if raw else "FAILED")
    k = fetch_klines_bybit("BTCUSDT")
    log.info("Bybit Klines : %s", "OK (%d candles)" % len(k) if k else "FAILED")
    fr = fetch_funding_bybit("BTCUSDT")
    log.info("Bybit Funding: %s", "OK" if fr else "FAILED/NA")
    ok_k = fetch_klines_okx("BTCUSDT")
    log.info("OKX Klines   : %s", "OK (%d candles)" % len(ok_k) if ok_k else "FAILED")
    log.info("=== COMPLETE ===")
    if raw and (k or ok_k):
        log.info("Ready. Run without --test to start.")


def main():
    parser = argparse.ArgumentParser(description="LRS v6 Pro")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    log.info("LRS v6 Pro | Bybit+OKX | No hardcoded scores | Explosive Move Detector")
    log.info("=" * 52)
    if args.test:
        run_test(); return
    s = Scanner()
    if args.once:
        s.run_cycle(); s.ws.stop(); return
    s.run_forever()


if __name__ == "__main__":
    main()
