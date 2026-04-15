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
    "min_signal_strength": 3.9,
    "strong_signal_threshold": 4.4,
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
    except:
        pass

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

# ====================== Altcoin Strength Filter (20%) ======================
def get_altcoin_strength(coins):
    try:
        filtered = [c for c in coins if c.get("market_cap") and 
                   DEFAULT_CONFIG["market_cap_min"] <= c.get("market_cap", 0) <= DEFAULT_CONFIG["market_cap_max"]]

        if len(filtered) < 30:
            return {"strength": 0.5, "status": "Neutral"}

        g1 = filtered[0:10]
        g2 = filtered[10:50]
        g3 = filtered[50:100]
        g4 = filtered[100:]

        def group_score(g):
            if not g: 
                return 0.5
            positive = sum(1 for c in g if c.get("price_change_percentage_24h", 0) > 0)
            avg_chg = sum(c.get("price_change_percentage_24h", 0) for c in g) / len(g)
            return (positive / len(g)) * 0.6 + (1 if avg_chg > 2 else 0.7 if avg_chg > 0 else 0.2)

        s1 = group_score(g1)
        s2 = group_score(g2)
        s3 = group_score(g3)
        s4 = group_score(g4)

        strength = (s1 + s2 + s3 + s4) / 4
        strength = round(min(max(strength, 0.0), 1.0), 2)

        status = "Strong" if strength >= 0.75 else "Moderate" if strength >= 0.55 else "Neutral" if strength >= 0.40 else "Weak"

        print(f"🌍 Altcoin Strength: {status} | Score: {strength}")

        return {"strength": strength, "status": status}

    except Exception as e:
        print(f"⚠️ خطأ في Altcoin Strength: {e}")
        return {"strength": 0.55, "status": "Neutral"}

# ====================== Liquidity Zones (28%) ======================
def get_liquidity_score(coin):
    try:
        price = coin.get("current_price", 0)
        if price == 0:
            return 0.5
        # تقريبي لأدنى سعر خلال 7 أيام
        low_7d = price * 0.88  # افتراضي
        distance = (price - low_7d) / price
        score = max(0.3, 1.0 - distance * 1.8)
        return round(score, 2)
    except:
        return 0.65

# ====================== Volume Confirmation نسبي (20%) ======================
def get_volume_score(coin, coins):
    try:
        current_vol = coin.get("total_volume", 0)
        if current_vol == 0:
            return 0.3

        avg_vol = sum(c.get("total_volume", 0) for c in coins[:100]) / 100 if coins else current_vol
        ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

        if ratio >= 4.0:
            return 0.95
        elif ratio >= 3.0:
            return 0.85
        elif ratio >= 2.0:
            return 0.70
        elif ratio >= 1.5:
            return 0.50
        else:
            return 0.30
    except:
        return 0.45

# ====================== التحليل الرئيسي ======================
def analyze_coin(coin, cfg, alt_strength, coins):
    symbol = coin.get("symbol", "").upper()
    name = coin.get("name", "")
    price = coin.get("current_price", 0)
    market_cap = coin.get("market_cap", 0) or 0
    volume_24h = coin.get("total_volume", 0) or 0

    if not (cfg["market_cap_min"] <= market_cap <= cfg["market_cap_max"]):
        return None

    liquidity_score = get_liquidity_score(coin)
    volume_score = get_volume_score(coin, coins)
    sector_score = 0.75

    # التقييم النهائي حسب الأوزان
    final_strength = (
        0.28 * liquidity_score +
        0.20 * volume_score +
        0.20 * alt_strength.get("strength", 0.55) +
        0.15 * 0.75 +      # Order Flow
        0.10 * sector_score +
        0.07 * 0.65        # Social
    )
    final_strength = round(final_strength * 5, 1)

    if final_strength < cfg["min_signal_strength"] and final_strength < cfg["strong_signal_threshold"]:
        return None

    confidence = "High" if final_strength >= 4.4 else "Medium" if final_strength >= 3.9 else "Low"

    signal = {
        "symbol": symbol,
        "name": name,
        "price": price,
        "market_cap": market_cap,
        "strength": final_strength,
        "confidence": confidence,
        "direction": "Long",
        "expectation": "صعودي (Liquidity Rotation)",
        "sector": "غير محدد",
        "reason": f"Alt: {alt_strength.get('status')} | Liq + Vol قوي",
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
    send_telegram("🤖 Liquidity Rotation Scanner v3\n✅ تم تشغيل النسخة الاحترافية", cfg)
    print("✅ النظام v3 يعمل الآن - النسخة الاحترافية...")

    last_alert = {}
    last_strength = {}

    while True:
        try:
            coins = get_coins_market()
            alt_strength = get_altcoin_strength(coins)

            print(f"جاري تحليل {len(coins)} عملة...")

            alert_count = 0
            for coin in coins:
                signal = analyze_coin(coin, cfg, alt_strength, coins)
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

                    if current_strength >= cfg["strong_signal_threshold"] or strength_diff >= 0.5 or time_diff >= cfg["alert_cooldown_hours"] * 3600:
                        should_send = True

                if should_send:
                    alert_msg = generate_alert(signal)
                    if alert_msg:
                        send_telegram(alert_msg, cfg)
                        print(f"✅ تم إرسال تنبيه: {symbol} | قوة {current_strength}/5")
                        alert_count += 1

                        last_alert[symbol] = current_time
                        last_strength[symbol] = current_strength

                time.sleep(0.4)

            print(f"✅ انتهى الفحص - تم إرسال {alert_count} تنبيه")

        except Exception as e:
            print(f"❌ خطأ عام: {e}")
            time.sleep(60)

        time.sleep(cfg.get("check_interval", 300))

if __name__ == "__main__":
    main()
