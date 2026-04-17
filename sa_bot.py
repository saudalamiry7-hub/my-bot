"""
Liquidity Rotation Scanner v5 Professional
CoinGecko + Bybit + OKX fallback
Smart API usage: Bybit/OKX only for high-scoring candidates
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
    # --- TELEGRAM ---
    # BOT_TOKEN: Telegram -> @BotFather -> /newbot
    # CHAT_ID:   https://api.telegram.org/bot<TOKEN>/getUpdates
    "TELEGRAM_BOT_TOKEN": "8509548153:AAEdsqKFuALjrTEgU8f8wExvm2fIf1Y9dig",
    "TELEGRAM_CHAT_ID":   "873875241",
 
    # --- DATA SOURCES ---
    "COINGECKO_BASE": "https://api.coingecko.com/api/v3",
    "BYBIT_BASE":     "https://api.bybit.com",
    "OKX_BASE":       "https://www.okx.com",
    "BYBIT_WS":       "wss://stream.bybit.com/v5/public/spot",
 
    # --- SCAN SETTINGS ---
    "TOP_N_COINS":       300,
    "SCAN_INTERVAL_SEC": 1500,
 
    # --- MARKET CAP FILTERS ---
    "MC_MID_MIN":          80_000_000,
    "MC_MID_MAX":        1_000_000_000,
    "MC_SMALL_MIN":        20_000_000,
    "MC_SMALL_MAX":        79_999_999,
    "SMALL_CAP_MIN_SCORE": 3.9,
 
    # --- ALERT THRESHOLDS ---
    "ALERT_MIN":    3.0,
    "ALERT_STRONG": 3.7,
    "ALERT_ULTRA":  4.5,
 
    # --- COOLDOWN (minutes) ---
    "COOLDOWN_MODERATE_MIN": 120,
    "COOLDOWN_STRONG_MIN":    20,
    "COOLDOWN_ULTRA_MIN":     10,
    "SCORE_JUMP_OVERRIDE":   0.3,
 
    # --- SMART ENRICHMENT ---
    # Only coins scoring >= this in the previous cycle get Bybit/OKX data
    "ENRICH_MIN_SCORE": 3.0,
 
    # --- WEBSOCKET ---
    "WS_WATCH_MIN_SCORE":   3.0,
    "WS_PRICE_SPIKE_PCT":   3.0,
    "WS_VOLUME_SPIKE_MULT": 3.0,
    "WS_MAX_SYMBOLS":       40,
 
    # --- RATE LIMITING ---
    "BYBIT_REQUEST_DELAY":     0.25,
    "OKX_REQUEST_DELAY":       0.25,
    "COINGECKO_REQUEST_DELAY": 2.8,
    "BUILD_LIST_DELAY":        0.8,
    "MAX_RETRIES":             3,
    "RETRY_BASE_DELAY":        4,
 
    # --- KLINES CACHE ---
    "KLINES_CACHE_TTL_SEC":   21600,   # 6 hours
    "KLINES_CACHE_CLEAN_SEC":  1800,   # clean every 30 minutes
    "KLINES_DAYS":               30,
 
    # --- LOGGING ---
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
class CoinData:
    id:     str
    symbol: str
    name:   str
    rank:   int
    price:            float
    market_cap:       float
    volume_24h:       float
    price_change_1h:  float
    price_change_24h: float
    price_change_7d:  float
    ath:              float
    atl:              float
    volume_7d_avg:      float = 0.0
    volume_30d_avg:     float = 0.0
    volume_spike_ratio: float = 0.0
    price_volatility:   float = 0.0
    atr_pct:            float = 0.0
    recent_low:         float = 0.0
    recent_high:        float = 0.0
    funding_rate:       float = 0.0
    funding_rate_avg7d: float = 0.0
    open_interest:      float = 0.0
    oi_change_24h:      float = 0.0
    bid_ask_ratio:      float = 1.0
    dev_score:           float = 0.0
    community_score:     float = 0.0
    commit_acceleration: float = 0.0
    sector:              str  = "Other"
    has_derivative_data: bool = False
    has_ohlcv_data:      bool = False
    data_source:         str  = "CoinGecko"
    exchange_symbol:     str  = ""
 
 
@dataclass
class DimensionScore:
    name:       str
    score:      float
    weight:     float
    signals:    list = field(default_factory=list)
    sub_scores: dict = field(default_factory=dict)
 
 
@dataclass
class MarketRegime:
    btc_trend_short:  str   = "neutral"
    btc_trend_medium: str   = "neutral"
    altcoin_breadth:  float = 0.5
    altcoins_state:   str   = "neutral"
    market_volume:    str   = "normal"
    regime:           str   = "neutral"
    confidence:       str   = "low"
    timestamp:        datetime = field(default_factory=datetime.utcnow)
 
 
@dataclass
class CoinAnalysis:
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
    regime:         MarketRegime = field(default_factory=MarketRegime)
    timestamp:      datetime = field(default_factory=datetime.utcnow)
 
    def score_label(self) -> str:
        s = self.final_score
        if s >= 4.5: return "ULTRA"
        if s >= 4.0: return "STRONG"
        if s >= 3.5: return "MODERATE"
        return "WATCH"
 
 
# =============================================================================
# KLINES CACHE  (6-hour TTL, auto-clean every 30 min)
# =============================================================================
 
class KlinesCache:
 
    def __init__(self):
        self._cache      = {}
        self._lock       = threading.Lock()
        self._last_clean = datetime.utcnow()
 
    def get(self, symbol: str):
        self._maybe_clean()
        with self._lock:
            entry = self._cache.get(symbol)
            if entry is None:
                return None
            age = (datetime.utcnow() - entry["ts"]).total_seconds()
            if age > CONFIG["KLINES_CACHE_TTL_SEC"]:
                del self._cache[symbol]
                return None
            return entry["data"]
 
    def set(self, symbol: str, data: list, source: str = "bybit"):
        with self._lock:
            self._cache[symbol] = {
                "data": data,
                "ts":   datetime.utcnow(),
                "src":  source,
            }
 
    def _maybe_clean(self):
        now = datetime.utcnow()
        if (now - self._last_clean).total_seconds() < CONFIG["KLINES_CACHE_CLEAN_SEC"]:
            return
        with self._lock:
            expired = [
                k for k, v in self._cache.items()
                if (now - v["ts"]).total_seconds() > CONFIG["KLINES_CACHE_TTL_SEC"]
            ]
            for k in expired:
                del self._cache[k]
            if expired:
                log.info("[Cache] Cleaned %d expired entries", len(expired))
        self._last_clean = now
 
    def size(self) -> int:
        with self._lock:
            return len(self._cache)
 
 
KLINES_CACHE = KlinesCache()
 
 
# =============================================================================
# SECTOR CLASSIFIER
# =============================================================================
 
SECTOR_MAP = {
    "AI":          ["ai", "artificial", "intelligence", "neural", "fetch",
                    "ocean", "singularity", "render", "akash", "gpt", "agi", "bittensor"],
    "RWA":         ["rwa", "ondo", "centrifuge", "maple", "goldfinch", "creditcoin"],
    "DeFi":        ["defi", "swap", "finance", "yield", "liquidity", "protocol",
                    "curve", "aave", "compound", "uniswap", "dydx", "gmx", "pendle"],
    "Gaming":      ["game", "gaming", "play", "metaverse", "nft", "axie",
                    "sandbox", "decentraland", "gala", "illuvium", "imx", "beam", "ronin"],
    "Meme":        ["doge", "shib", "pepe", "floki", "bonk", "meme",
                    "wojak", "inu", "elon", "moon", "cat", "frog"],
    "Layer1":      ["avalanche", "solana", "near", "aptos", "sui", "sei",
                    "injective", "cosmos", "algorand"],
    "Layer2":      ["optimism", "arbitrum", "polygon", "zk", "rollup",
                    "blast", "base", "scroll", "starknet", "linea"],
    "Oracle":      ["oracle", "chainlink", "band", "api3", "uma", "tellor", "pyth"],
    "LiquidStake": ["lido", "rocket", "frax", "ankr", "eigen", "restaking"],
    "Storage":     ["filecoin", "arweave", "sia", "storj"],
}
 
 
def classify_sector(coin_id: str, symbol: str, name: str) -> str:
    text = (coin_id + " " + symbol + " " + name).lower()
    for sector, keywords in SECTOR_MAP.items():
        if any(kw in text for kw in keywords):
            return sector
    return "Other"
 
 
# =============================================================================
# HTTP HELPER
# =============================================================================
 
def _http_get(url: str, params: dict = None, delay: float = 0.0, source: str = "API"):
    if delay > 0:
        time.sleep(delay)
    for attempt in range(CONFIG["MAX_RETRIES"]):
        try:
            r = requests.get(url, params=params,
                             headers={"accept": "application/json"}, timeout=12)
            if r.status_code == 429:
                wait = CONFIG["RETRY_BASE_DELAY"] * (2 ** attempt) + 5
                log.warning("[%s] Rate limit -> waiting %ds", source, wait)
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
            log.warning("[%s] Error attempt %d: %s", source, attempt + 1, e)
        time.sleep(CONFIG["RETRY_BASE_DELAY"] * (2 ** attempt))
    log.error("[%s] All retries failed: %s", source, url)
    return None
 
 
# =============================================================================
# COINGECKO FETCHER
# =============================================================================
 
class CoinGeckoFetcher:
 
    BASE  = CONFIG["COINGECKO_BASE"]
    DELAY = CONFIG["COINGECKO_REQUEST_DELAY"]
 
    def fetch_markets_page(self, page: int, per_page: int = 150) -> list:
        return _http_get(
            self.BASE + "/coins/markets",
            params={
                "vs_currency":             "usd",
                "order":                   "market_cap_desc",
                "per_page":                per_page,
                "page":                    page,
                "sparkline":               "false",
                "price_change_percentage": "1h,24h,7d",
            },
            delay=self.DELAY if page > 1 else 0,
            source="CoinGecko"
        ) or []
 
    def fetch_top300(self) -> list:
        log.info("[CoinGecko] Fetching top 300 coins...")
        p1 = self.fetch_markets_page(1, 150)
        p2 = self.fetch_markets_page(2, 150)
        result = p1 + p2
        log.info("[CoinGecko] Fetched %d coins", len(result))
        return result
 
    def fetch_btc_data(self) -> dict:
        data = _http_get(
            self.BASE + "/coins/markets",
            params={
                "vs_currency":             "usd",
                "ids":                     "bitcoin",
                "price_change_percentage": "1h,24h,7d",
            },
            delay=0,
            source="CoinGecko-BTC"
        )
        if data and len(data) > 0:
            return data[0]
        return {}
 
 
# =============================================================================
# BYBIT FETCHER
# =============================================================================
 
class BybitFetcher:
 
    BASE  = CONFIG["BYBIT_BASE"]
    DELAY = CONFIG["BYBIT_REQUEST_DELAY"]
 
    SYMBOL_MAP = {
        "bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "binancecoin": "BNBUSDT",
        "ripple": "XRPUSDT", "solana": "SOLUSDT", "dogecoin": "DOGEUSDT",
        "cardano": "ADAUSDT", "tron": "TRXUSDT", "avalanche-2": "AVAXUSDT",
        "shiba-inu": "SHIBUSDT", "chainlink": "LINKUSDT", "polkadot": "DOTUSDT",
        "near": "NEARUSDT", "uniswap": "UNIUSDT", "litecoin": "LTCUSDT",
        "stellar": "XLMUSDT", "injective-protocol": "INJUSDT",
        "sei-network": "SEIUSDT", "render-token": "RENDERUSDT",
        "fetch-ai": "FETUSDT", "ondo-finance": "ONDOUSDT",
        "pendle": "PENDLEUSDT", "arbitrum": "ARBUSDT", "optimism": "OPUSDT",
        "starknet": "STRKUSDT", "blur": "BLURUSDT", "jupiter-ag": "JUPUSDT",
        "sui": "SUIUSDT", "aptos": "APTUSDT", "celestia": "TIAUSDT",
        "worldcoin-wld": "WLDUSDT", "pyth-network": "PYTHUSDT",
    }
 
    def get_symbol(self, coin_id: str, symbol: str) -> str:
        return self.SYMBOL_MAP.get(coin_id, symbol.upper() + "USDT")
 
    def fetch_klines(self, symbol: str, interval: str = "D", limit: int = 30) -> list:
        data = _http_get(
            self.BASE + "/v5/market/kline",
            params={"category": "spot", "symbol": symbol,
                    "interval": interval, "limit": limit},
            delay=self.DELAY,
            source="Bybit-Klines"
        )
        if not data:
            return []
        rows = data.get("result", {}).get("list", [])
        return list(reversed(rows))
 
    def fetch_funding_rate(self, symbol: str, limit: int = 50) -> list:
        data = _http_get(
            self.BASE + "/v5/market/funding/history",
            params={"category": "linear", "symbol": symbol, "limit": limit},
            delay=self.DELAY,
            source="Bybit-Funding"
        )
        if not data:
            return []
        return data.get("result", {}).get("list", [])
 
    def fetch_open_interest(self, symbol: str) -> dict:
        data = _http_get(
            self.BASE + "/v5/market/open-interest",
            params={"category": "linear", "symbol": symbol,
                    "intervalTime": "1d", "limit": 2},
            delay=self.DELAY,
            source="Bybit-OI"
        )
        if not data:
            return {}
        rows = data.get("result", {}).get("list", [])
        return rows[0] if rows else {}
 
 
# =============================================================================
# OKX FETCHER (fallback)
# =============================================================================
 
class OKXFetcher:
 
    BASE  = CONFIG["OKX_BASE"]
    DELAY = CONFIG["OKX_REQUEST_DELAY"]
 
    def get_symbol(self, symbol: str) -> str:
        if symbol.endswith("USDT"):
            return symbol[:-4] + "-USDT"
        return symbol
 
    def fetch_klines(self, symbol: str, bar: str = "1D", limit: int = 30) -> list:
        okx_sym = self.get_symbol(symbol)
        data = _http_get(
            self.BASE + "/api/v5/market/candles",
            params={"instId": okx_sym, "bar": bar, "limit": str(limit)},
            delay=self.DELAY,
            source="OKX-Klines"
        )
        if not data:
            return []
        return list(reversed(data.get("data", [])))
 
    def fetch_funding_rate(self, symbol: str, limit: int = 50) -> list:
        okx_sym = self.get_symbol(symbol)
        swap_id = okx_sym.replace("-USDT", "-USDT-SWAP")
        data = _http_get(
            self.BASE + "/api/v5/public/funding-rate-history",
            params={"instId": swap_id, "limit": str(limit)},
            delay=self.DELAY,
            source="OKX-Funding"
        )
        if not data:
            return []
        return data.get("data", [])
 
 
# =============================================================================
# DATA ENRICHER
# Smart enrichment: Bybit/OKX only for previous-cycle candidates (score >= 3.0)
# All others use CoinGecko baseline only.
# On cycle 1, no candidates exist yet, so all coins use CoinGecko only.
# =============================================================================
 
class DataEnricher:
 
    def __init__(self):
        self.cg    = CoinGeckoFetcher()
        self.bybit = BybitFetcher()
        self.okx   = OKXFetcher()
 
    # --- stat helpers --------------------------------------------------------
 
    def _volume_stats(self, klines: list) -> dict:
        if len(klines) < 3:
            return {}
        try:
            volumes = [float(k[5]) for k in klines]
            closes  = [float(k[4]) for k in klines]
            highs   = [float(k[2]) for k in klines]
            lows    = [float(k[3]) for k in klines]
            returns = [
                (closes[i] - closes[i-1]) / closes[i-1]
                for i in range(1, len(closes)) if closes[i-1] > 0
            ]
            vol_7d    = statistics.mean(volumes[-7:])  if len(volumes) >= 7  else statistics.mean(volumes)
            vol_30d   = statistics.mean(volumes[-30:]) if len(volumes) >= 30 else statistics.mean(volumes)
            vol_today = volumes[-1] if volumes else 0
            spike     = vol_today / vol_30d if vol_30d > 0 else 1.0
            volatility = statistics.stdev(returns) if len(returns) >= 2 else 0.02
            tr_vals = []
            for i in range(1, min(15, len(klines))):
                h, l, pc = highs[i], lows[i], closes[i-1]
                tr = max(h - l, abs(h - pc), abs(l - pc))
                if closes[i] > 0:
                    tr_vals.append(tr / closes[i])
            atr_pct    = statistics.mean(tr_vals) if tr_vals else volatility
            last5_lows  = lows[-5:]  if len(lows)  >= 5 else lows
            last5_highs = highs[-5:] if len(highs) >= 5 else highs
            return {
                "volume_7d_avg":      vol_7d,
                "volume_30d_avg":     vol_30d,
                "volume_spike_ratio": spike,
                "price_volatility":   volatility,
                "atr_pct":            atr_pct,
                "recent_low":         min(last5_lows)  if last5_lows  else 0,
                "recent_high":        max(last5_highs) if last5_highs else 0,
            }
        except Exception as e:
            log.debug("volume_stats error: %s", e)
            return {}
 
    def _funding_stats(self, rows: list, source: str = "bybit") -> dict:
        if not rows:
            return {}
        try:
            key   = "fundingRate" if source == "bybit" else "realizedRate"
            rates = [float(r.get(key, 0)) * 100 for r in rows]
            if not rates:
                return {}
            current = rates[0]
            avg_7d  = statistics.mean(rates[:21]) if len(rates) >= 3 else current
            return {"funding_rate": current, "funding_rate_avg7d": avg_7d}
        except Exception:
            return {}
 
    def _oi_change(self, oi_row: dict) -> dict:
        if not oi_row:
            return {}
        try:
            oi_val = float(oi_row.get("openInterest", oi_row.get("oi", 0)))
            return {"open_interest": oi_val, "oi_change_24h": 0.0}
        except Exception:
            return {}
 
    def _apply_klines(self, coin: CoinData, klines: list):
        s = self._volume_stats(klines)
        if s:
            coin.has_ohlcv_data     = True
            coin.volume_7d_avg      = s.get("volume_7d_avg", 0)
            coin.volume_30d_avg     = s.get("volume_30d_avg", 0)
            coin.volume_spike_ratio = s.get("volume_spike_ratio", 1.0)
            coin.price_volatility   = s.get("price_volatility", 0.02)
            coin.atr_pct            = s.get("atr_pct", 0.03)
            coin.recent_low         = s.get("recent_low", 0)
            coin.recent_high        = s.get("recent_high", 0)
 
    # --- enrichment paths ----------------------------------------------------
 
    def _enrich_full(self, coin: CoinData):
        """
        Full enrichment via Bybit, with OKX fallback.
        Used only for coins that scored >= ENRICH_MIN_SCORE last cycle.
        """
        sym = coin.exchange_symbol
 
        # --- Klines (cached) ---
        cached = KLINES_CACHE.get(sym)
        if cached:
            self._apply_klines(coin, cached)
            log.debug("[Cache] klines hit for %s", sym)
        else:
            klines = self.bybit.fetch_klines(sym, "D", CONFIG["KLINES_DAYS"])
            if klines:
                KLINES_CACHE.set(sym, klines, "bybit")
                self._apply_klines(coin, klines)
                coin.data_source = "Bybit"
            else:
                klines = self.okx.fetch_klines(sym, "1D", CONFIG["KLINES_DAYS"])
                if klines:
                    KLINES_CACHE.set(sym, klines, "okx")
                    self._apply_klines(coin, klines)
                    coin.data_source = "OKX"
 
        # --- Funding rate ---
        fr = self.bybit.fetch_funding_rate(sym)
        if not fr:
            fr = self.okx.fetch_funding_rate(sym)
            source = "okx"
        else:
            source = "bybit"
        if fr:
            coin.has_derivative_data = True
            s = self._funding_stats(fr, source)
            coin.funding_rate       = s.get("funding_rate", 0)
            coin.funding_rate_avg7d = s.get("funding_rate_avg7d", 0)
 
        # --- Open interest ---
        oi = self.bybit.fetch_open_interest(sym)
        if oi:
            s = self._oi_change(oi)
            coin.open_interest = s.get("open_interest", 0)
            coin.oi_change_24h = s.get("oi_change_24h", 0)
 
    def _enrich_baseline(self, coin: CoinData):
        """
        Baseline enrichment from CoinGecko data only (no extra API calls).
        Estimates volume stats from 24h data already available.
        """
        if coin.volume_24h > 0 and coin.market_cap > 0:
            vol_mc_ratio = coin.volume_24h / coin.market_cap
            coin.volume_spike_ratio = min(3.0, vol_mc_ratio * 8)
            coin.price_volatility   = max(0.02, abs(coin.price_change_24h) / 100 * 0.5)
            coin.atr_pct            = coin.price_volatility * 1.2
        coin.data_source = "CoinGecko"
 
    def enrich(self, raw: dict, full_enrich: bool = False) -> CoinData:
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
        coin.exchange_symbol = self.bybit.get_symbol(coin_id, symbol)
 
        if full_enrich:
            self._enrich_full(coin)
        else:
            self._enrich_baseline(coin)
 
        return coin
 
    def build_list(self, raw_list: list, candidates: set) -> list:
        """
        candidates: set of symbols that scored >= ENRICH_MIN_SCORE last cycle.
        These get full Bybit/OKX enrichment.
        All others get CoinGecko baseline only.
        """
        total  = len(raw_list)
        result = []
 
        for i, raw in enumerate(raw_list, 1):
            sym = raw.get("symbol", "").upper()
            try:
                full = sym in candidates
                coin = self.enrich(raw, full_enrich=full)
                result.append(coin)
                if i % 10 == 0:
                    log.info("  [%d/%d] cache=%d  full_enriched=%s",
                             i, total, KLINES_CACHE.size(),
                             sym if full else "-")
                if full:
                    time.sleep(CONFIG["BUILD_LIST_DELAY"])
            except Exception as e:
                log.warning("Enrichment error %s: %s", raw.get("symbol", "?"), e)
 
        full_count = sum(1 for c in result if c.has_ohlcv_data)
        log.info("Enrichment complete: %d coins  (%d full / %d baseline)",
                 len(result), full_count, len(result) - full_count)
        return result
 
 
# =============================================================================
# MARKET REGIME ANALYZER  (zero weight, context only)
# =============================================================================
 
class MarketRegimeAnalyzer:
 
    def __init__(self):
        self.cg = CoinGeckoFetcher()
 
    def analyze(self, coins: list) -> MarketRegime:
        regime = MarketRegime()
        try:
            btc = self.cg.fetch_btc_data()
            if btc:
                ch1h = float(btc.get("price_change_percentage_1h_in_currency") or 0)
                ch7d = float(btc.get("price_change_percentage_7d_in_currency") or 0)
                regime.btc_trend_short  = "bullish" if ch1h > 0.5  else ("bearish" if ch1h < -0.5 else "neutral")
                regime.btc_trend_medium = "bullish" if ch7d > 3    else ("bearish" if ch7d < -3   else "neutral")
 
            if coins:
                up    = sum(1 for c in coins if c.price_change_24h > 2)
                total = len(coins)
                regime.altcoin_breadth = up / total if total > 0 else 0.5
                if regime.altcoin_breadth > 0.55:   regime.altcoins_state = "expanding"
                elif regime.altcoin_breadth < 0.35: regime.altcoins_state = "contracting"
                else:                                regime.altcoins_state = "neutral"
 
                vols = [c.volume_24h for c in coins if c.volume_24h > 0]
                if len(vols) > 1:
                    ratio = statistics.mean(vols) / statistics.median(vols)
                    regime.market_volume = "high" if ratio > 1.5 else ("low" if ratio < 0.7 else "normal")
 
            bull, bear = 0, 0
            if regime.btc_trend_medium == "bullish": bull += 2
            if regime.btc_trend_medium == "bearish": bear += 2
            if regime.btc_trend_short  == "bullish": bull += 1
            if regime.btc_trend_short  == "bearish": bear += 1
            if regime.altcoins_state == "expanding":   bull += 2
            if regime.altcoins_state == "contracting": bear += 2
            if regime.market_volume == "high": bull += 1
 
            if bull >= 4:
                regime.regime     = "risk_on"
                regime.confidence = "high" if bull >= 5 else "medium"
            elif bear >= 4:
                regime.regime     = "risk_off"
                regime.confidence = "high" if bear >= 5 else "medium"
            else:
                regime.regime     = "neutral"
                regime.confidence = "low"
 
        except Exception as e:
            log.warning("Regime analysis error: %s", e)
        return regime
 
 
# =============================================================================
# SCANNER ENGINE - 6 DIMENSIONS
# Weights: D1=18% D2=22% D3=27% D4=13% D5=12% D6=8%
# =============================================================================
 
class SectorMomentumTracker:
 
    def __init__(self):
        self._changes = defaultdict(list)
        self._volumes = defaultdict(list)
        self._market_avg = 0.0
 
    def update(self, coins: list):
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
        if not data: return 0.30
        relative = statistics.mean(data) - self._market_avg
        return round(max(0.0, min(1.0, 1 / (1 + math.exp(-relative / 3)))), 4)
 
    def sector_breadth(self, sector: str) -> float:
        data = self._changes.get(sector, [])
        if not data: return 0.20
        return min(1.0, sum(1 for x in data if x > 2.0) / len(data) * 1.5)
 
    def volume_surge(self, sector: str, coin_volume: float) -> float:
        vols = self._volumes.get(sector, [])
        if not vols or coin_volume == 0: return 0.30
        avg   = statistics.mean(vols)
        ratio = coin_volume / avg if avg > 0 else 1.0
        return min(1.0, math.log10(max(ratio, 0.1) + 1) / math.log10(6))
 
 
SECTOR_TRACKER = SectorMomentumTracker()
 
 
# D1 - Liquidity Zones (18%) -------------------------------------------------
 
class D1_LiquidityZones:
    WEIGHT = 0.18
 
    def score(self, c: CoinData) -> DimensionScore:
        signals, sub = [], {}
 
        s1 = 0.0
        if c.ath > 0 and c.price > 0:
            dist = (c.ath - c.price) / c.ath
            if 0.50 <= dist <= 0.80:
                s1 = max(0.0, 1.0 - abs(dist - 0.65) * 3)
                signals.append("OB Zone (%.0f%% below ATH)" % (dist * 100))
            elif 0.80 < dist <= 0.95:
                s1 = 0.70
                signals.append("Deep Value (%.0f%% below ATH)" % (dist * 100))
            elif 0.20 <= dist < 0.50: s1 = 0.40
            elif dist < 0.10:         s1 = 0.15; signals.append("Near ATH - resistance")
        sub["order_block"] = round(s1, 3)
 
        s2 = 0.0
        if c.price_change_24h != 0:
            ratio = abs(c.price_change_1h) / (abs(c.price_change_24h) + 0.01)
            if ratio > 0.25:
                s2 = min(1.0, ratio * 1.5)
                signals.append("FVG Signal (1h=%.1f%% / 24h=%.1f%%)" % (c.price_change_1h, c.price_change_24h))
            if c.price_change_7d < -10 and c.price_change_1h > 1.0:
                s2 = max(s2, 0.65)
                signals.append("FVG Bounce (7d=%.1f%%)" % c.price_change_7d)
        sub["fvg"] = round(s2, 3)
 
        s3 = 0.0
        if c.price_change_7d <= -20:
            s3 = 0.50 + min(0.50, abs(c.price_change_7d + 20) / 30)
            signals.append("Short Squeeze Candidate (%.1f%% 7d)" % c.price_change_7d)
        elif -20 < c.price_change_7d <= -10: s3 = 0.35
        sub["squeeze_zone"] = round(s3, 3)
 
        s4 = 0.0
        if c.atl > 0 and c.price > 0:
            mult = c.price / c.atl
            if 2.0 <= mult <= 5.0:   s4 = 0.70; signals.append("Accumulation Zone (%.1fx ATL)" % mult)
            elif 1.05 <= mult < 2.0: s4 = 0.85; signals.append("Near ATL - high risk/reward")
            elif mult > 10:           s4 = 0.30
        sub["atl_zone"] = round(s4, 3)
 
        final = round(min(1.0, s1 * 0.35 + s2 * 0.25 + s3 * 0.25 + s4 * 0.15), 4)
        return DimensionScore("Liquidity Zones", final, self.WEIGHT, signals, sub)
 
 
# D2 - Smart Money / OI / Funding (22%) --------------------------------------
# Primary: Funding Rate + OI + Absorption. Order book removed entirely.
 
class D2_SmartMoney:
    WEIGHT = 0.22
 
    def score(self, c: CoinData) -> DimensionScore:
        signals, sub = [], {}
 
        # Funding rate divergence from 7d average
        s1 = 0.35
        if c.has_derivative_data:
            dev = c.funding_rate - c.funding_rate_avg7d
            if dev < -0.03:
                s1 = min(1.0, 0.65 + abs(dev) * 15)
                signals.append("Negative Funding Divergence (%.4f%%)" % dev)
            elif -0.03 <= dev < -0.01:
                s1 = 0.60; signals.append("Mildly Negative Funding")
            elif dev > 0.05:
                s1 = 0.20; signals.append("High Positive Funding - longs crowded")
            elif abs(dev) < 0.01:
                s1 = 0.50
        sub["funding_divergence"] = round(s1, 3)
 
        # Open interest change
        s2 = 0.35
        if c.has_derivative_data and c.open_interest > 0:
            oi = c.oi_change_24h
            if 5 <= oi <= 30:    s2 = min(1.0, 0.55 + oi / 50); signals.append("OI Inflow +%.1f%%" % oi)
            elif oi > 30:         s2 = 0.50; signals.append("OI Spike %.1f%%" % oi)
            elif oi < -10:        s2 = 0.20; signals.append("OI Outflow %.1f%%" % oi)
            else:                 s2 = 0.40
        sub["oi_change"] = round(s2, 3)
 
        # Absorption: price drops on high volume = smart money buying
        s3 = 0.0
        if c.price_change_24h < -5 and c.volume_spike_ratio > 1.5:
            s3 = min(1.0, (abs(c.price_change_24h) / 15) * (c.volume_spike_ratio / 3))
            signals.append("Absorption (%.1f%% / %.1fx vol)" % (c.price_change_24h, c.volume_spike_ratio))
        elif c.price_change_24h > 5 and c.volume_spike_ratio > 2.0:
            s3 = min(1.0, 0.50 + (c.volume_spike_ratio - 2) * 0.15)
            signals.append("Volume Breakout (%.1fx)" % c.volume_spike_ratio)
        sub["absorption"] = round(s3, 3)
 
        # Short squeeze alignment: negative funding + OI rising
        s4 = 0.30
        if c.has_derivative_data:
            if c.funding_rate < -0.01 and c.oi_change_24h > 5:
                s4 = min(1.0, 0.75 + abs(c.funding_rate) * 10)
                signals.append("Short Squeeze Setup (FR=%.4f / OI=+%.1f%%)" % (c.funding_rate, c.oi_change_24h))
            elif c.funding_rate > 0.05 and c.oi_change_24h < -5:
                s4 = 0.20; signals.append("Long Squeeze Risk")
            elif abs(c.funding_rate) < 0.01 and c.oi_change_24h > 0:
                s4 = 0.55
        sub["oi_funding_align"] = round(s4, 3)
 
        final = round(min(1.0, s1 * 0.30 + s2 * 0.25 + s3 * 0.25 + s4 * 0.20), 4)
        return DimensionScore("Smart Money / OI / Funding", final, self.WEIGHT, signals, sub)
 
 
# D3 - Volume Confirmation (27%) - Highest weight. Core of explosive detector.
 
class D3_VolumeConfirmation:
    WEIGHT = 0.27
 
    def score(self, c: CoinData) -> DimensionScore:
        signals, sub = [], {}
 
        s1 = 0.0
        if c.volume_30d_avg > 0:
            sp30 = c.volume_24h / c.volume_30d_avg
            sp7  = c.volume_24h / c.volume_7d_avg if c.volume_7d_avg > 0 else 1.0
            if 2.0 <= sp30 <= 6.0:
                s1 = 0.60 + min(0.40, (sp30 - 2) / 4 * 0.40)
                signals.append("Vol Spike %.1fx (30d avg)" % sp30)
            elif 1.5 <= sp30 < 2.0: s1 = 0.50
            elif sp30 > 6.0:         s1 = 0.45; signals.append("Extreme Vol %.1fx" % sp30)
            else:                    s1 = max(0.0, sp30 * 0.20)
            if sp7 > 1.8 and sp30 > 1.8:
                s1 = min(1.0, s1 + 0.10)
                signals.append("Vol Consistency (7d=%.1fx / 30d=%.1fx)" % (sp7, sp30))
        else:
            vol_mc = c.volume_24h / max(c.market_cap, 1)
            s1 = min(0.55, vol_mc * 2)
        sub["volume_spike"] = round(s1, 3)
 
        # VCP: low ATR + volume expansion = compression before explosion
        s2 = 0.0
        atr = c.atr_pct if c.atr_pct > 0 else c.price_volatility
        if atr > 0:
            if atr < 0.025 and c.volume_spike_ratio > 2.0:
                s2 = min(1.0, (2.5 - atr * 40) * (c.volume_spike_ratio / 4))
                signals.append("VCP Compression (ATR=%.1f%% / spike=%.1fx)" % (atr * 100, c.volume_spike_ratio))
            elif atr < 0.03 and c.volume_spike_ratio > 1.5:
                s2 = 0.65; signals.append("Low ATR + Vol Expansion")
            elif 0.03 <= atr <= 0.06: s2 = 0.40
            else: s2 = max(0.0, 0.25 - (atr - 0.06) * 2)
        sub["vcp_compression"] = round(s2, 3)
 
        # Volume confirms price direction
        s3 = 0.35
        if c.price_change_24h > 3 and c.volume_spike_ratio > 1.5:
            s3 = min(1.0, (c.price_change_24h / 15) * (c.volume_spike_ratio / 3))
            signals.append("Vol/Price Breakout Confirmed")
        elif c.price_change_24h > 3 and c.volume_spike_ratio < 1.0:
            s3 = 0.20; signals.append("Price up / Vol weak")
        elif c.price_change_24h < -3 and c.volume_spike_ratio < 0.8:
            s3 = 0.60; signals.append("Exhaustion sell")
        sub["price_vol_confirm"] = round(s3, 3)
 
        # Relative volume rank (normalized across all coins in this cycle)
        s4 = 0.40
        sub["relative_rank"] = round(s4, 3)
 
        final = round(min(1.0, s1 * 0.40 + s2 * 0.30 + s3 * 0.20 + s4 * 0.10), 4)
        return DimensionScore("Volume Confirmation", final, self.WEIGHT, signals, sub)
 
 
# D4 - Order Flow Clusters (13%) - No order book fetches. Structure-based only.
 
class D4_OrderFlowClusters:
    WEIGHT = 0.13
 
    def score(self, c: CoinData) -> DimensionScore:
        signals, sub = [], {}
 
        # Fibonacci cluster proximity (uses ATH/ATL already available)
        s1 = 0.30
        if c.ath > c.atl > 0:
            pct   = (c.price - c.atl) / (c.ath - c.atl)
            min_d = min(abs(pct - f) for f in [0.236, 0.382, 0.500, 0.618, 0.786])
            if min_d < 0.03:
                s1 = min(1.0, 0.70 + (0.03 - min_d) * 15)
                signals.append("Near Fib Level (%.1f%% of range)" % (pct * 100))
            elif min_d < 0.07: s1 = 0.55
        sub["fib_cluster"] = round(s1, 3)
 
        # Recent candle structure: where is price in the last 5 candles
        s2 = 0.35
        if c.recent_low > 0 and c.recent_high > 0 and c.price > 0:
            rng = c.recent_high - c.recent_low
            if rng > 0:
                pos = (c.price - c.recent_low) / rng
                if pos < 0.25:
                    s2 = 0.80; signals.append("Price at range bottom - structural support")
                elif pos > 0.80:
                    s2 = 0.70; signals.append("Breaking range top - breakout attempt")
                else:
                    s2 = 0.40
        sub["price_structure"] = round(s2, 3)
 
        # Bid/ask proxy from CoinGecko volume ratio (no order book fetch needed)
        # High 1h volume relative to 24h avg = buying pressure signal
        s3 = 0.35
        if c.price_change_1h > 1.0 and c.volume_spike_ratio > 1.5:
            s3 = min(1.0, 0.55 + (c.price_change_1h / 10) * (c.volume_spike_ratio / 3))
            signals.append("1h momentum + vol surge")
        elif c.price_change_1h < -1.0 and c.volume_spike_ratio > 1.5:
            s3 = 0.25
        sub["momentum_proxy"] = round(s3, 3)
 
        final = round(min(1.0, s1 * 0.35 + s2 * 0.40 + s3 * 0.25), 4)
        return DimensionScore("Order Flow Clusters", final, self.WEIGHT, signals, sub)
 
 
# D5 - Sector Momentum (12%) --------------------------------------------------
 
class D5_SectorMomentum:
    WEIGHT = 0.12
 
    def score(self, c: CoinData) -> DimensionScore:
        signals = []
        rs  = SECTOR_TRACKER.relative_strength(c.sector)
        brd = SECTOR_TRACKER.sector_breadth(c.sector)
        vs  = SECTOR_TRACKER.volume_surge(c.sector, c.volume_24h)
        if rs  > 0.70: signals.append("Sector [%s] outperforming market" % c.sector)
        if brd > 0.60: signals.append("Broad [%s] rally (%.0f%% up)" % (c.sector, brd * 100))
        if vs  > 0.70: signals.append("Above-sector volume surge")
        final = round(min(1.0, rs * 0.45 + brd * 0.35 + vs * 0.20), 4)
        return DimensionScore("Sector Momentum", final, self.WEIGHT, signals,
                              {"rs": round(rs, 3), "breadth": round(brd, 3), "surge": round(vs, 3)})
 
 
# D6 - OnChain Health (8%) ----------------------------------------------------
 
class D6_OnChainHealth:
    WEIGHT = 0.08
 
    def score(self, c: CoinData) -> DimensionScore:
        signals, sub = [], {}
 
        s1 = 0.0
        if c.dev_score > 0:
            s1 = min(1.0, c.dev_score / 80)
            if c.commit_acceleration > 0.5:
                s1 = min(1.0, s1 + 0.20); signals.append("Dev Acceleration (%.0f)" % c.dev_score)
            else:
                signals.append("Dev Activity: %.0f/100" % c.dev_score)
        sub["dev"] = round(s1, 3)
 
        s2 = min(1.0, c.community_score / 75) if c.community_score > 0 else 0.0
        if s2 > 0: signals.append("Community: %.0f/100" % c.community_score)
        sub["community"] = round(s2, 3)
 
        s3 = 0.35
        if c.has_derivative_data:
            fr = c.funding_rate
            if -0.05 < fr < -0.01:   s3 = 0.75; signals.append("Healthy Negative Funding (%.4f%%)" % fr)
            elif -0.10 < fr <= -0.05: s3 = 0.85; signals.append("Strong Negative Funding")
            elif 0 <= fr <= 0.02:     s3 = 0.55
            elif fr > 0.05:           s3 = 0.20; signals.append("Crowded Longs")
        sub["funding_sentiment"] = round(s3, 3)
 
        s4 = 0.35
        if c.has_derivative_data and c.open_interest > 0:
            oi = c.oi_change_24h
            if 3 <= oi <= 20: s4 = min(1.0, 0.55 + oi / 40); signals.append("OI Growing +%.1f%%" % oi)
            elif oi < -5:     s4 = 0.20
        sub["oi_trend"] = round(s4, 3)
 
        final = round(min(1.0, s1 * 0.25 + s2 * 0.15 + s3 * 0.35 + s4 * 0.25), 4)
        return DimensionScore("OnChain Health", final, self.WEIGHT, signals, sub)
 
 
# --- Scan Engine -------------------------------------------------------------
 
class ScanEngine:
 
    def __init__(self):
        self.d1 = D1_LiquidityZones()
        self.d2 = D2_SmartMoney()
        self.d3 = D3_VolumeConfirmation()
        self.d4 = D4_OrderFlowClusters()
        self.d5 = D5_SectorMomentum()
        self.d6 = D6_OnChainHealth()
 
    def _data_quality(self, c: CoinData) -> str:
        if c.has_ohlcv_data and c.has_derivative_data: return "FULL (%s)" % c.data_source
        if c.has_ohlcv_data: return "PARTIAL (%s)" % c.data_source
        return "LIMITED (baseline)"
 
    def _confidence(self, score: float, quality: str) -> str:
        penalty = "LIMITED" in quality
        for thr, lbl in [(4.5, "ULTRA"), (4.0, "HIGH"), (3.5, "MEDIUM"), (0, "LOW")]:
            if score >= thr:
                if penalty and lbl in ("ULTRA", "HIGH"):
                    lbl = {"ULTRA": "HIGH", "HIGH": "MEDIUM"}[lbl]
                return lbl
        return "LOW"
 
    def _risk(self, c: CoinData) -> str:
        atr = c.atr_pct if c.atr_pct > 0 else c.price_volatility
        if atr > 0.06 or c.market_cap < CONFIG["MC_SMALL_MAX"] or abs(c.funding_rate) > 0.08: return "HIGH"
        if atr > 0.03 or c.market_cap < 200_000_000 or abs(c.funding_rate) > 0.04: return "MEDIUM"
        return "LOW"
 
    def _entry_scenario(self, c: CoinData, dims: list) -> str:
        sigs = [s for d in dims for s in d.signals]
        if any("VCP" in s or "Compression" in s for s in sigs) and any("Breakout" in s for s in sigs):
            return "VCP breakout - entry on volume confirm above compression zone"
        if any("Squeeze" in s for s in sigs):
            return "Short squeeze setup - scale into dips, target liquidation zones"
        if any("Absorption" in s for s in sigs):
            return "Absorption confirmed - wait for reversal candle then entry"
        if dims[0].score > 0.70 and dims[1].score > 0.65:
            return "OB reclaim with smart money alignment - entry on stabilization"
        if dims[2].score > 0.75:
            return "Volume breakout - entry on retest of breakout level"
        return "Support retest with volume - entry with confirmation candle"
 
    def _sl_tp(self, c: CoinData, risk: str, score: float):
        atr = c.atr_pct if c.atr_pct > 0 else max(c.price_volatility, 0.03)
        if c.recent_low > 0 and c.price > c.recent_low:
            structure_sl = (c.price - c.recent_low) / c.price * 100 + atr * 50
            sl = max(3.0, min(15.0, structure_sl))
        else:
            sl = max(3.5, min(12.0, atr * 150))
        if risk == "HIGH": sl = min(15.0, sl * 1.2)
        if c.recent_high > c.price > 0:
            tp1_natural = (c.recent_high - c.price) / c.price * 100
            tp1 = max(sl * 1.3, min(tp1_natural, sl * 3.0))
        else:
            tp1 = round(sl * (1.5 + (score - 3.5) * 0.4), 1)
        tp2 = round(sl * (2.5 + (score - 3.5) * 0.8), 1)
        return round(sl, 1), round(tp1, 1), tp2
 
    def _build(self, coin: CoinData, dims: list, regime: MarketRegime) -> CoinAnalysis:
        weighted = sum(d.score * d.weight for d in dims)
        final    = round(weighted * 5, 2)
        quality  = self._data_quality(coin)
        risk     = self._risk(coin)
        sl, tp1, tp2 = self._sl_tp(coin, risk, final)
        return CoinAnalysis(
            coin=coin, dimensions=dims, final_score=final,
            confidence=self._confidence(final, quality),
            risk_level=risk, data_quality=quality,
            entry_scenario=self._entry_scenario(coin, dims),
            stop_loss_pct=sl, tp1_pct=tp1, tp2_pct=tp2, regime=regime,
        )
 
    def scan_all(self, coins: list, regime: MarketRegime) -> list:
        raw = []
        for c in coins:
            dims = [self.d1.score(c), self.d2.score(c), self.d3.score(c),
                    self.d4.score(c), self.d5.score(c), self.d6.score(c)]
            raw.append((c, dims))
 
        # Normalize relative volume rank across all scanned coins
        max_spike = max((c.volume_spike_ratio for c, _ in raw), default=1.0) or 1.0
        for c, dims in raw:
            rk = round(c.volume_spike_ratio / max_spike, 3)
            dims[2].sub_scores["relative_rank"] = rk
            dims[2].score = round(min(1.0, dims[2].score * 0.90 + rk * 0.10), 4)
 
        results = [self._build(c, dims, regime) for c, dims in raw]
        return sorted(results, key=lambda x: x.final_score, reverse=True)
 
 
# =============================================================================
# ALERT MANAGER + TELEGRAM
# =============================================================================
 
class CooldownManager:
 
    def __init__(self):
        self._history = {}
 
    def _cooldown(self, score: float) -> int:
        if score >= CONFIG["ALERT_ULTRA"]:  return CONFIG["COOLDOWN_ULTRA_MIN"]
        if score >= CONFIG["ALERT_STRONG"]: return CONFIG["COOLDOWN_STRONG_MIN"]
        return CONFIG["COOLDOWN_MODERATE_MIN"]
 
    def should_alert(self, symbol: str, score: float) -> tuple:
        entry = self._history.get(symbol)
        if entry is None: return True, "first_alert"
        elapsed = (datetime.utcnow() - entry["time"]).total_seconds() / 60
        if elapsed >= self._cooldown(entry["score"]): return True, "cooldown_expired"
        if score >= entry["score"] + CONFIG["SCORE_JUMP_OVERRIDE"]:
            return True, "score_jump +%.1f" % (score - entry["score"])
        return False, "cooldown (%.0f/%.0f min)" % (elapsed, self._cooldown(entry["score"]))
 
    def record(self, symbol: str, score: float):
        self._history[symbol] = {"score": score, "time": datetime.utcnow()}
 
 
class MessageFormatter:
 
    REGIME_MAP = {
        "risk_on":  "GREEN - RISK ON",
        "neutral":  "YELLOW - NEUTRAL",
        "risk_off": "RED - RISK OFF",
    }
 
    @staticmethod
    def _mc(mc: float) -> str:
        if mc >= 1e9: return "$%.2fB" % (mc / 1e9)
        if mc >= 1e6: return "$%.0fM" % (mc / 1e6)
        return "$%.0f" % mc
 
    @staticmethod
    def _bar(score: float) -> str:
        f = round(score / 5 * 10)
        return "[" + "#" * f + "-" * (10 - f) + "]"
 
    def format_alert(self, a: CoinAnalysis, reason: str = "") -> str:
        c, r = a.coin, a.regime
        now  = datetime.utcnow().strftime("%H:%M UTC")
        lines = [
            "*** %s ***" % a.score_label(),
            "=" * 36,
            "Coin   : %s  (%s)" % (c.name, c.symbol),
            "MC     : %s   Rank #%d" % (self._mc(c.market_cap), c.rank),
            "Price  : $%s" % ("%.4g" % c.price),
            "Sector : %s" % c.sector,
            "",
            "Signal : %.1f / 5.0   %s" % (a.final_score, self._bar(a.final_score)),
            "",
            "Dimensions:",
        ]
        for d in a.dimensions:
            bar = "#" * round(d.score * 5) + "-" * (5 - round(d.score * 5))
            lines.append("  [%s] %.2f  %s" % (bar, d.score, d.name))
        lines += [
            "",
            "Confidence : %-6s  Risk: %s" % (a.confidence, a.risk_level),
            "Data       : %s" % a.data_quality,
            "",
            "Entry:",
            "  %s" % a.entry_scenario,
            "",
            "SL: -%.1f%%   TP1: +%.1f%%   TP2: +%.1f%%" % (a.stop_loss_pct, a.tp1_pct, a.tp2_pct),
            "",
            "Change: 1h %+.1f%%  24h %+.1f%%  7d %+.1f%%" % (
                c.price_change_1h, c.price_change_24h, c.price_change_7d),
        ]
        top = [s for d in a.dimensions for s in d.signals[:1]][:4]
        if top:
            lines += ["", "Signals:"]
            for s in top:
                lines.append("  * %s" % s)
        lines += [
            "",
            "-- Market Context --",
            "Regime : %s" % self.REGIME_MAP.get(r.regime, r.regime),
            "BTC    : %s (short) / %s (med)" % (r.btc_trend_short.upper(), r.btc_trend_medium.upper()),
            "Alts   : %s   Vol: %s" % (r.altcoins_state.upper(), r.market_volume.upper()),
            "",
            "=" * 36,
            "%s  |  LRS v5 Pro" % now,
        ]
        if "score_jump" in reason:
            lines.append("^ Score upgraded")
        return "\n".join(lines)
 
    def format_summary(self, sent: list, duration: float,
                       total: int, regime: MarketRegime) -> str:
        now = datetime.utcnow().strftime("%H:%M UTC")
        lines = [
            "--- Scan Summary ---",
            "%s  |  %d coins  |  %.0fs" % (now, total, duration),
            "Alerts: %d   Market: %s" % (
                len(sent), self.REGIME_MAP.get(regime.regime, regime.regime)),
        ]
        if sent:
            lines.append("")
            for i, a in enumerate(sent[:5], 1):
                lines.append("  %d. %-8s [%s] %.1f" % (
                    i, a.coin.symbol, a.score_label(), a.final_score))
        lines.append("-" * 28)
        return "\n".join(lines)
 
 
class TelegramSender:
 
    API = "https://api.telegram.org/bot%s/sendMessage"
 
    def __init__(self):
        self.token   = CONFIG["TELEGRAM_BOT_TOKEN"]
        self.chat_id = CONFIG["TELEGRAM_CHAT_ID"]
        if "PUT_YOUR" in str(self.token):
            log.error("Telegram not configured - set BOT_TOKEN and CHAT_ID in CONFIG")
 
    def send(self, text: str) -> bool:
        url = self.API % self.token
        for attempt in range(3):
            try:
                r = requests.post(url, json={
                    "chat_id": self.chat_id, "text": text,
                    "disable_web_page_preview": True,
                }, timeout=10)
                if r.status_code == 200: return True
                log.warning("Telegram HTTP %d: %s", r.status_code, r.text[:60])
            except requests.RequestException as e:
                log.warning("Telegram error attempt %d: %s", attempt + 1, e)
            time.sleep(2 ** attempt)
        return False
 
    def test_connection(self) -> bool:
        ok = self.send("LRS v5 Pro - Connection OK. System starting.")
        log.info("Telegram: %s", "OK" if ok else "FAILED")
        return ok
 
 
class AlertManager:
 
    def __init__(self):
        self.cooldown  = CooldownManager()
        self.formatter = MessageFormatter()
        self.telegram  = TelegramSender()
        self._sent     = 0
 
    def process(self, analyses: list, duration: float = 0,
                total: int = 0, regime: MarketRegime = None) -> list:
        if regime is None: regime = MarketRegime()
        sent = []
        for a in analyses:
            c, sc = a.coin, a.final_score
            in_mid   = CONFIG["MC_MID_MIN"] <= c.market_cap <= CONFIG["MC_MID_MAX"]
            in_small = CONFIG["MC_SMALL_MIN"] <= c.market_cap <= CONFIG["MC_SMALL_MAX"]
            if not (in_mid or in_small): continue
            if in_small and sc < CONFIG["SMALL_CAP_MIN_SCORE"]: continue
            if sc < CONFIG["ALERT_MIN"]: break
            ok, reason = self.cooldown.should_alert(c.symbol, sc)
            if not ok:
                log.debug("SKIP %-8s %.1f - %s", c.symbol, sc, reason)
                continue
            if self.telegram.send(self.formatter.format_alert(a, reason)):
                self.cooldown.record(c.symbol, sc)
                sent.append(a)
                self._sent += 1
                log.info("ALERT: %-8s [%s] %.1f  (%s)",
                         c.symbol, a.score_label(), sc, reason)
                time.sleep(1.5)
        self.telegram.send(
            self.formatter.format_summary(sent, duration, total, regime))
        return sent
 
 
# =============================================================================
# WEBSOCKET MONITOR (Bybit)
# Starts only when there are coins to watch (score >= 3.0)
# =============================================================================
 
class LiveCoinState:
 
    def __init__(self, symbol: str, baseline_price: float, baseline_vol: float):
        self.symbol          = symbol
        self.baseline_price  = baseline_price
        self.baseline_vol    = baseline_vol
        self.current_price   = baseline_price
        self.current_vol     = 0.0
        self.price_history   = deque(maxlen=20)
        self.alert_triggered = False
 
    def update(self, price: float, vol: float):
        self.current_price = price
        self.current_vol   = vol
        self.price_history.append(price)
 
    def recent_change_pct(self) -> float:
        if len(self.price_history) < 5: return 0.0
        old = list(self.price_history)[-5]
        return (self.current_price - old) / old * 100 if old else 0.0
 
    def vol_spike_ratio(self) -> float:
        per_min = self.baseline_vol / 1440
        return self.current_vol / per_min if per_min > 0 else 1.0
 
 
class WebSocketMonitor:
 
    WS_URL = CONFIG["BYBIT_WS"]
 
    def __init__(self):
        self._states  = {}
        self._ws      = None
        self._thread  = None
        self._running = False
        self._lock    = threading.Lock()
        self._reconnect_delay = 5
        self.on_spike = None
 
    def update_watchlist(self, symbol_map: dict):
        if not symbol_map:
            log.info("[WS] No candidates for watchlist - stream idle")
            return
        limited = dict(list(symbol_map.items())[:CONFIG["WS_MAX_SYMBOLS"]])
        with self._lock:
            for sym, (price, vol) in limited.items():
                key = sym.lower()
                if key not in self._states:
                    self._states[key] = LiveCoinState(sym, price, vol)
            for k in [k for k in self._states if k.upper() not in limited]:
                del self._states[k]
        log.info("[WS] Watchlist: %d coins", len(self._states))
        if self._running:
            self._resubscribe()
        elif self._states:
            self.start()
 
    def _build_sub(self) -> dict:
        with self._lock:
            syms = [k.upper() for k in self._states.keys()]
        return {"op": "subscribe",
                "args": ["tickers.%s" % s for s in syms[:CONFIG["WS_MAX_SYMBOLS"]]]}
 
    def _on_message(self, ws, message):
        try:
            data  = json.loads(message)
            topic = data.get("topic", "")
            if not topic.startswith("tickers."): return
            ticker = data.get("data", {})
            sym    = ticker.get("symbol", "").lower()
            price  = float(ticker.get("lastPrice", 0) or 0)
            vol    = float(ticker.get("volume24h", 0) or 0)
            with self._lock:
                state = self._states.get(sym)
            if not state or price == 0: return
            state.update(price, vol)
            self._check_spike(sym, state)
        except Exception:
            pass
 
    def _check_spike(self, sym: str, state: LiveCoinState):
        pm = abs(state.recent_change_pct())
        vs = state.vol_spike_ratio()
        reasons = []
        if pm >= CONFIG["WS_PRICE_SPIKE_PCT"]:  reasons.append("price=%.1f%%" % pm)
        if vs >= CONFIG["WS_VOLUME_SPIKE_MULT"]: reasons.append("vol=%.1fx" % vs)
        if reasons and not state.alert_triggered:
            state.alert_triggered = True
            log.info("[WS] Spike: %s - %s", sym.upper(), ", ".join(reasons))
            if self.on_spike:
                self.on_spike(sym.upper(), {
                    "symbol": sym.upper(), "price": state.current_price,
                    "price_chg": state.recent_change_pct(),
                    "vol_spike": vs, "reasons": reasons,
                })
            def _reset():
                time.sleep(300); state.alert_triggered = False
            threading.Thread(target=_reset, daemon=True).start()
 
    def _resubscribe(self):
        if self._ws:
            try: self._ws.send(json.dumps(self._build_sub()))
            except Exception: pass
 
    def _on_open(self, ws):
        self._reconnect_delay = 5
        log.info("[WS] Connected to Bybit")
        self._resubscribe()
 
    def _on_error(self, ws, error): log.warning("[WS] Error: %s", error)
 
    def _on_close(self, ws, code, msg):
        log.info("[WS] Disconnected (%s)", code)
        if self._running:
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(60, self._reconnect_delay * 2)
            self._connect()
 
    def _connect(self):
        self._ws = websocket.WebSocketApp(
            self.WS_URL,
            on_message=self._on_message, on_error=self._on_error,
            on_close=self._on_close,    on_open=self._on_open,
        )
        self._ws.run_forever(ping_interval=20, ping_timeout=10)
 
    def start(self):
        if self._running: return
        self._running = True
        self._thread  = threading.Thread(target=self._connect, daemon=True, name="WS")
        self._thread.start()
        log.info("[WS] Monitor started (Bybit)")
 
    def stop(self):
        self._running = False
        if self._ws: self._ws.close()
 
 
# =============================================================================
# MAIN LOOP
# =============================================================================
 
STABLE_KEYWORDS = [
    "usd", "usdt", "usdc", "busd", "dai", "tusd",
    "frax", "lusd", "wrapped", "staked", "wbtc", "weth", "pax",
]
 
 
def filter_coins(raw: list) -> list:
    result = []
    for coin in raw:
        mc  = float(coin.get("market_cap") or 0)
        sym = coin.get("symbol", "").lower()
        cid = coin.get("id", "").lower()
        if any(kw in sym or kw in cid for kw in STABLE_KEYWORDS): continue
        if (CONFIG["MC_MID_MIN"] <= mc <= CONFIG["MC_MID_MAX"] or
                CONFIG["MC_SMALL_MIN"] <= mc <= CONFIG["MC_SMALL_MAX"]):
            result.append(coin)
    return result
 
 
class LiquidityScanner:
 
    def __init__(self):
        log.info("Initializing LRS v5 Pro...")
        self.cg       = CoinGeckoFetcher()
        self.enricher = DataEnricher()
        self.engine   = ScanEngine()
        self.alerts   = AlertManager()
        self.regime_a = MarketRegimeAnalyzer()
        self.ws       = WebSocketMonitor()
        self.ws.on_spike  = self._on_ws_spike
        # candidates: symbols eligible for full enrichment next cycle
        self._candidates: set = set()
        self._last_analyses  = []
        self._cycle          = 0
        log.info("Ready.")
 
    def _on_ws_spike(self, symbol: str, data: dict):
        match = next((a for a in self._last_analyses
                      if a.coin.exchange_symbol == symbol), None)
        if not match: return
        msg = (
            "*** WS SPIKE: %s (%s) ***\n"
            "Price  : $%.4g  (%+.1f%%)\n"
            "Volume : %.1fx above normal\n"
            "Reason : %s\n"
            "Score  : [%s] %.1f / 5.0\n"
            "%s  |  LRS v5 Pro"
        ) % (
            match.coin.name, match.coin.symbol,
            data["price"], data["price_chg"],
            data["vol_spike"],
            ", ".join(data["reasons"]),
            match.score_label(), match.final_score,
            datetime.utcnow().strftime("%H:%M UTC"),
        )
        self.alerts.telegram.send(msg)
 
    def _update_candidates(self, analyses: list):
        """
        Update the set of symbols that will receive full Bybit/OKX
        enrichment in the NEXT cycle (scored >= ENRICH_MIN_SCORE).
        """
        self._candidates = {
            a.coin.symbol
            for a in analyses
            if a.final_score >= CONFIG["ENRICH_MIN_SCORE"]
        }
        log.info("Candidates for next cycle full enrichment: %d coins",
                 len(self._candidates))
 
    def _update_ws(self, analyses: list):
        wl = {
            a.coin.exchange_symbol: (
                a.coin.price,
                a.coin.volume_30d_avg or a.coin.volume_24h,
            )
            for a in analyses
            if a.final_score >= CONFIG["WS_WATCH_MIN_SCORE"]
            and a.coin.exchange_symbol
        }
        top = dict(list(wl.items())[:CONFIG["WS_MAX_SYMBOLS"]])
        self.ws.update_watchlist(top)
 
    def run_cycle(self) -> list:
        self._cycle += 1
        start = time.time()
        log.info("=" * 52)
        log.info("Cycle #%d  %s  (cache: %d  candidates: %d)",
                 self._cycle,
                 datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                 KLINES_CACHE.size(),
                 len(self._candidates))
        log.info("=" * 52)
        try:
            # 1. Fetch top 300 from CoinGecko
            raw = self.cg.fetch_top300()
            if not raw:
                log.error("CoinGecko returned no data - skipping cycle")
                return []
 
            # 2. Filter by MC and remove stables
            filtered = filter_coins(raw)
            log.info("After filter: %d coins", len(filtered))
 
            # 3. Enrich - full only for previous-cycle candidates
            coins = self.enricher.build_list(filtered, self._candidates)
 
            # 4. Update sector tracker
            SECTOR_TRACKER.update(coins)
 
            # 5. Market regime (zero weight, context only)
            regime = self.regime_a.analyze(coins)
            log.info("Regime: %s | BTC: %s | Alts: %s",
                     regime.regime.upper(),
                     regime.btc_trend_medium.upper(),
                     regime.altcoins_state.upper())
 
            # 6. Score all coins
            log.info("Scoring %d coins...", len(coins))
            analyses = self.engine.scan_all(coins, regime)
 
            above = sum(1 for a in analyses if a.final_score >= CONFIG["ALERT_MIN"])
            log.info("Above threshold (%.1f): %d coins",
                     CONFIG["ALERT_MIN"], above)
 
            # 7. Send alerts
            duration = time.time() - start
            sent = self.alerts.process(analyses, duration, len(coins), regime)
 
            # 8. Update candidates and WebSocket for next cycle
            self._last_analyses = analyses
            self._update_candidates(analyses)
            self._update_ws(analyses)
 
            log.info("Cycle #%d done - %.0fs - %d alerts sent",
                     self._cycle, duration, len(sent))
            return analyses
 
        except Exception as e:
            log.error("Cycle #%d error: %s\n%s",
                      self._cycle, e, traceback.format_exc())
            return []
 
    def run_forever(self):
        log.info("Starting continuous scan loop...")
        if not self.alerts.telegram.test_connection():
            log.error("Telegram failed. Fix BOT_TOKEN and CHAT_ID then restart.")
            sys.exit(1)
        interval = CONFIG["SCAN_INTERVAL_SEC"]
        log.info("Scan interval: %d minutes", interval // 60)
        while True:
            try:
                self.run_cycle()
                log.info("Waiting %d minutes...", interval // 60)
                time.sleep(interval)
            except KeyboardInterrupt:
                log.info("Stopped by user.")
                self.ws.stop()
                break
            except Exception as e:
                log.error("Main loop error: %s", e)
                time.sleep(60)
 
 
# =============================================================================
# TEST MODE
# =============================================================================
 
def run_test():
    log.info("=== TEST MODE ===")
    tg = TelegramSender()
    tg_ok = tg.test_connection()
    if not tg_ok:
        log.error("Fix TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in CONFIG")
    cg  = CoinGeckoFetcher()
    raw = cg.fetch_markets_page(1, 5)
    log.info("CoinGecko    : %s", "OK (%d coins)" % len(raw) if raw else "FAILED")
    by  = BybitFetcher()
    k   = by.fetch_klines("BTCUSDT", "D", 3)
    log.info("Bybit Klines : %s", "OK (%d candles)" % len(k) if k else "FAILED")
    fr  = by.fetch_funding_rate("BTCUSDT", 3)
    log.info("Bybit Funding: %s", "OK" if fr else "FAILED/NA")
    ok  = OKXFetcher()
    ok_k = ok.fetch_klines("BTCUSDT", "1D", 3)
    log.info("OKX Klines   : %s", "OK (%d candles)" % len(ok_k) if ok_k else "FAILED")
    log.info("=== TEST COMPLETE ===")
    if tg_ok and raw and (k or ok_k):
        log.info("All systems OK. Run without --test to start.")
    else:
        log.warning("Some connections failed - check logs above.")
 
 
# =============================================================================
# ENTRY POINT
# =============================================================================
 
def main():
    parser = argparse.ArgumentParser(description="Liquidity Rotation Scanner v5 Pro")
    parser.add_argument("--test", action="store_true", help="Test connections and exit")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()
    log.info("LRS v5 Pro | Bybit+OKX fallback | Smart enrichment | Explosive Move Detector")
    log.info("=" * 52)
    if args.test:
        run_test()
        return
    scanner = LiquidityScanner()
    if args.once:
        scanner.run_cycle()
        scanner.ws.stop()
        return
    scanner.run_forever()
 
 
if __name__ == "__main__":
    main()
 
