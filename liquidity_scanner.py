import requests
import time
import json
from datetime import datetime

# ====================== إعدادات النظام ======================
CONFIG_FILE = "scanner_config.json"

DEFAULT_CONFIG = {
    "market_cap_min": 150000000,
    "market_cap_max": 900000000,
    "check_interval": 300,
    "alert_cooldown_hours": 6,
    "min_signal_strength": 3.8,
    "strong_signal_threshold": 4.3,
    "active_sectors_weight": 1.5,
    "telegram_token": "8509548153:AAE1nrJeE9u9x9MEQvYr-MvEo7wNE5YfYfE",
    "chat_id": "873875241",
    "active_sectors": [
        "ai", "artificial-intelligence", "gaming", "rwa", "real-world-assets",
        "defi", "layer-2"
    ]
}

def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return DEFAULT_CONFIG

def send_telegram(message, cfg):
    token = cfg.get("telegram_token", "")
    chat_id = cfg.get("chat_id", "")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=15)
    except Exception as e:
        print(f"خطأ في إرسال تيليغرام: {e}")

# ====================== جلب بيانات العملات ======================
def get_coins_market():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "volume_desc",
                "per_page": 500,
                "page": 1,
                "price_change_percentage": "24h,7d"
            },
            timeout=25
        )
        data = r.json()
        print(f"✅ تم جلب {len(data)} عملة بنجاح")
        return data
    except Exception as e:
        print(f"❌ خطأ في جلب بيانات CoinGecko: {e}")
        return []

# ====================== Market Sentiment Filter (المحسن والمصحح) ======================
def get_market_sentiment():
    try:
        # Total Market Cap
        global_data = requests.get("https://api.coingecko.com/api/v3/global", timeout=15).json()["data"]
        market_cap_change_24h = global_data.get("market_cap_change_percentage_24h_usd", 0)
        btc_dominance = global_data["market_cap_percentage"]["btc"]

        # ETH/BTC Ratio Change
        prices = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=ethereum,bitcoin&vs_currencies=usd&include_24hr_change=true",
            timeout=15
        ).json()
        eth_change = prices.get("ethereum", {}).get("usd_24h_change", 0)
        btc_change = prices.get("bitcoin", {}).get("usd_24h_change", 0)
        eth_btc_ratio_change = eth_change - btc_change if eth_change and btc_change else 0

        # Market Breadth
        coins = get_coins_market()
        positive_coins = sum(1 for c in coins if c.get("price_change_percentage_24h", 0) > 0)
        market_breadth = positive_coins / len(coins) if coins else 0.5

        # حساب Sentiment Score
        score = 0.0
        score += 0.35 * (1 if market_cap_change_24h > 0.5 else 0.5 if market_cap_change_24h > -0.5 else 0.0)
        score += 0.30 * (1 if eth_btc_ratio_change > 0 else 0.5 if eth_btc_ratio_change > -1 else 0.0)
        score += 0.20 * (1 if market_breadth > 0.55 else 0.6 if market_breadth > 0.45 else 0.2)
        score += 0.15 * (1 if btc_dominance < 53 else 0.6 if btc_dominance < 55.5 else 0.2)

        score = round(min(max(score, 0.0), 1.0), 2)

        if score >= 0.75:
            sentiment = "Bullish"
        elif score >= 0.45:
            sentiment = "Neutral"
        else:
            sentiment = "Bearish"

        print(f"🌍 Market Sentiment: {sentiment} | Score: {score} | BTC Dom: {btc_dominance:.2f}%")

        return {
            "sentiment": sentiment,
            "score": score,
            "btc_dominance": round(btc_dominance, 2),
            "market_cap_change": round(market_cap_change_24h, 2),
            "eth_btc_ratio_change": round(eth_btc_ratio_change, 2),
            "market_breadth": round(market_breadth, 2)
        }

    except Exception as e:
        print(f"⚠️ خطأ في جلب Market Sentiment: {e}")
        return {
            "sentiment": "Neutral",
            "score": 0.65,
            "btc_dominance": 52.0,
            "market_cap_change": 0,
            "eth_btc_ratio_change": 0,
            "market_breadth": 0.5
        }

# ====================== التحليل الرئيسي ======================
def analyze_coin(coin, cfg, sentiment):
    symbol = coin.get("symbol", "").upper()
    name = coin.get("name", "")
    price = coin.get("current_price", 0)
    market_cap = coin.get("market_cap", 0) or 0
    volume_24h = coin.get("total_volume", 0) or 0

    if not (cfg["market_cap_min"] <= market_cap <= cfg["market_cap_max"]):
        return None

    liquidity_score = 0.85
    squeeze_score = 0.75
    volume_score = 0.7 if volume_24h > 60000000 else 0.4

    base_strength = (liquidity_score + squeeze_score + volume_score) / 3 * 5
    sentiment_score = sentiment.get("score", 0.65) if isinstance(sentiment, dict) else 0.65
    strength = round(min(base_strength * sentiment_score, 5.0), 1)

    if strength < cfg["min_signal_strength"] and strength < cfg["strong_signal_threshold"]:
        return None

    confidence = "High" if strength >= 4.3 else "Medium" if strength >= 3.8 else "Low"

    signal = {
        "symbol": symbol,
        "name": name,
        "price": price,
        "market_cap": market_cap,
        "strength": strength,
        "confidence": confidence,
        "direction": "Long",
        "expectation": "صعودي (Liquidity Rotation)",
        "sector": "غير محدد",
        "reason": f"Sentiment: {sentiment.get('sentiment', 'Neutral')} (Score: {sentiment_score})",
        "risk": "متوسطة",
        "timeframe": "12-48 ساعة",
        "entry": f"{price*0.99:.4f} - {price*1.02:.4f}",
        "stop_loss": f"{price*0.95:.4f}",
        "tp1": f"{price*1.08:.4f}",
        "tp2": f"{price*1.15:.4f}"
    }
    return signal

# ====================== توليد التنبيه ======================
def generate_alert(signal):
    msg = f"""🔥 إشارة {signal["direction"]} - قوة {signal["strength"]:.1f}/5

{signal["name"]} ({signal["symbol"]}) • ${signal["price"]:.4f} • MC: ${signal["market_cap"]/1000000:.0f}M

التوقع: {signal["expectation"]}
الثقة: {signal["confidence"]}
المخاطرة: {signal["risk"]}
الوقت: {signal["timeframe"]}

سيناريو:
دخول: {signal["entry"]}
SL: {signal["stop_loss"]}
TP1: {signal["tp1"]}
TP2: {signal["tp2"]}
"""
    return msg

# ====================== الدورة الرئيسية ======================
def main():
    cfg = load_config()
    send_telegram("🤖 Liquidity Rotation Scanner v2.5\n✅ تم تشغيل النظام مع Market Sentiment Filter", cfg)
    print("✅ النظام v2.5 يعمل الآن مع Market Sentiment Filter...")

    last_alert = {}
    last_strength = {}

    while True:
        try:
            sentiment = get_market_sentiment()
            coins = get_coins_market()

            print(f"جاري تحليل {len(coins)} عملة...")

            alert_count = 0
            for coin in coins:
                signal = analyze_coin(coin, cfg, sentiment)
                if not signal:
                    continue

                symbol = signal["symbol"]
                current_strength = signal["strength"]
                current_time = time.time()

                should_send = False

                if symbol not in last_alert:
                    should_send = True
                else:
                    time_diff = current_time - last_alert.get(symbol, 0)
                    strength_diff = current_strength - last_strength.get(symbol, 0)

                    if current_strength >= cfg["strong_signal_threshold"]:
                        should_send = True
                    elif strength_diff >= 0.5 or time_diff >= cfg["alert_cooldown_hours"] * 3600:
                        should_send = True

                if should_send:
                    alert_msg = generate_alert(signal)
                    if alert_msg:
                        send_telegram(alert_msg, cfg)
                        print(f"✅ تم إرسال تنبيه: {symbol} | قوة {current_strength}/5")
                        alert_count += 1

                        last_alert[symbol] = current_time
                        last_strength[symbol] = current_strength

                time.sleep(0.5)

            print(f"✅ انتهى الفحص - تم إرسال {alert_count} تنبيه")

        except Exception as e:
            print(f"❌ خطأ عام: {e}")
            time.sleep(60)

        time.sleep(cfg.get("check_interval", 300))

if __name__ == "__main__":
    main()
