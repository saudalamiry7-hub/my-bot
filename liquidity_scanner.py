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
    "min_signal_strength": 3.0,
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

# ====================== جمع البيانات ======================
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
            timeout=20
        )
        return r.json()
    except Exception as e:
        print(f"خطأ في جلب بيانات CoinGecko: {e}")
        return []

# ====================== التحليل الرئيسي ======================
def analyze_coin(coin, cfg):
    symbol = coin.get("symbol", "").upper()
    name = coin.get("name", "")
    price = coin.get("current_price", 0)
    market_cap = coin.get("market_cap", 0) or 0
    change_24h = coin.get("price_change_percentage_24h", 0) or 0
    volume_24h = coin.get("total_volume", 0) or 0

    # 1. فلتر الماركت كاب
    if not (cfg["market_cap_min"] <= market_cap <= cfg["market_cap_max"]):
        return None

    # 2. Regime Filter (مؤقتاً نفترض إيجابي)
    regime_ok = True

    # 3. Liquidity Setup
    liquidity_score = 0.8

    # 4. Squeeze Signal
    squeeze_score = 0.7

    # 5. Volume Confirmation
    volume_score = 0.6 if volume_24h > 50000000 else 0.3

    # 6. Sector Strength
    sector = "غير محدد"
    # سنطور هذا الجزء لاحقاً

    # حساب قوة الإشارة من 5
    base_strength = (liquidity_score + squeeze_score + volume_score) / 3 * 5
    strength = round(min(base_strength * (cfg["active_sectors_weight"] if sector in cfg["active_sectors"] else 1.0), 5.0), 1)

    # مستوى الثقة
    if strength >= 4.0:
        confidence = "High"
    elif strength >= 3.0:
        confidence = "Medium"
    else:
        confidence = "Low"

    direction = "Long"
    expectation = "صعودي (احتمال Liquidity Grab)"

    signal = {
        "symbol": symbol,
        "name": name,
        "price": price,
        "market_cap": market_cap,
        "change_24h": change_24h,
        "strength": strength,
        "confidence": confidence,
        "direction": direction,
        "expectation": expectation,
        "sector": sector,
        "reason": "اقتراب من منطقة سيولة + حجم جيد",
        "risk": "متوسطة",
        "timeframe": "12-48 ساعة",
        "entry": f"{price*0.99:.4f} - {price*1.02:.4f}",
        "stop_loss": f"{price*0.96:.4f}",
        "tp1": f"{price*1.06:.4f}",
        "tp2": f"{price*1.12:.4f}"
    }

    return signal

# ====================== توليد التنبيه ======================
def generate_alert(signal):
    if not signal or signal["strength"] < 3.0:
        return None

    msg = f"""🔥 إشارة {signal["direction"]} - قوة {signal["strength"]:.1f}/5

{signal["name"]} ({signal["symbol"]}) • ${signal["price"]:.4f} • MC: ${signal["market_cap"]/1000000:.0f}M
قطاع: {signal["sector"]}

التوقع: {signal["expectation"]}

الثقة: {signal["confidence"]}
المخاطرة: {signal["risk"]}
الوقت المتوقع: {signal["timeframe"]}

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
    send_telegram("🤖 Liquidity Rotation Scanner v1\n✅ تم تشغيل النظام بنجاح", cfg)
    print("✅ النظام يعمل الآن...")

    while True:
        try:
            coins = get_coins_market()
            print(f"جاري فحص {len(coins)} عملة...")

            for coin in coins:
                signal = analyze_coin(coin, cfg)
                if not signal:
                    continue

                alert_msg = generate_alert(signal)
                if alert_msg:
                    send_telegram(alert_msg, cfg)
                    print(f"✅ تم إرسال تنبيه: {signal['symbol']} | قوة {signal['strength']}/5")

            time.sleep(cfg.get("check_interval", 300))

        except Exception as e:
            print(f"❌ خطأ: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
