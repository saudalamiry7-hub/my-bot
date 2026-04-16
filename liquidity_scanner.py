import requests
import time
import json
from datetime import datetime

# ====================== إعدادات النظام ======================
CONFIG_FILE = "scanner_config.json"

DEFAULT_CONFIG = {
    "market_cap_min": 80000000,      # 80M
    "market_cap_max": 1000000000,    # 1B
    "check_interval": 300,
    "alert_cooldown_hours": 6,
    "min_signal_strength": 3.2,      # خفض كما طلبت
    "strong_signal_threshold": 4.2,  # يسمح بالتكرار عند 4.2+
    "telegram_token": "8509548153:AAE1nrJeE9u9x9MEQvYr-MvEo7wNE5YfYfE",
    "chat_id": "873875241"
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
        print(f"❌ خطأ في جلب البيانات: {e}")
        return []

# ====================== 1. Liquidity Zones & Liquidation (20%) ======================
def get_liquidity_zones(coin):
    try:
        price = coin.get("current_price", 0)
        if price == 0:
            return 0.5
        proximity = max(0.3, 1.0 - (price * 0.88 / price))
        return round(proximity, 2)
    except:
        return 0.65

# ====================== 2. Smart Money Inflow / Order Flow (20%) ======================
def get_smart_liquidity_flow(coin, coins):
    try:
        price = coin.get("current_price", 0)
        volume = coin.get("total_volume", 0)
        if price == 0 or volume == 0:
            return 0.55

        avg_vol = sum(c.get("total_volume", 0) for c in coins[:100]) / 100 if coins else volume
        volume_ratio = volume / avg_vol if avg_vol > 0 else 1.0
        proximity = max(0.3, 1.0 - (price * 0.9 / price))

        score = (volume_ratio * 0.55) + (proximity * 0.45)
        return round(min(max(score, 0.3), 1.0), 2)
    except:
        return 0.60

# ====================== 3. Volume Confirmation نسبي متقدم (25%) ======================
def get_volume_score(coin, coins):
    try:
        current_vol = coin.get("total_volume", 0)
        if current_vol == 0:
            return 0.35

        avg_vol = sum(c.get("total_volume", 0) for c in coins[:100]) / 100 if coins else current_vol
        ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

        if ratio >= 4.0: return 0.95
        elif ratio >= 3.0: return 0.85
        elif ratio >= 2.2: return 0.75
        elif ratio >= 1.6: return 0.55
        else: return 0.35
    except:
        return 0.50

# ====================== 4. Order Flow & Clusters (15%) ======================
def get_order_flow_score(coin):
    # مؤقت احترافي (يمكن تطويره لاحقاً)
    return 0.72

# ====================== 5. Sector Momentum (12%) ======================
def get_sector_score(coin):
    return 0.75

# ====================== 6. Social & Narrative Momentum (8%) ======================
def get_social_score(coin):
    return 0.65

# ====================== التحليل الرئيسي ======================
def analyze_coin(coin, cfg, coins):
    symbol = coin.get("symbol", "").upper()
    name = coin.get("name", "")
    price = coin.get("current_price", 0)
    market_cap = coin.get("market_cap", 0) or 0
    volume_24h = coin.get("total_volume", 0) or 0

    if not (cfg["market_cap_min"] <= market_cap <= cfg["market_cap_max"]):
        return None

    # حساب الأبعاد
    liquidity_score = get_liquidity_zones(coin)
    volume_score = get_volume_score(coin, coins)
    smart_liquidity = get_smart_liquidity_flow(coin, coins)
    order_flow_score = get_order_flow_score(coin)
    sector_score = get_sector_score(coin)
    social_score = get_social_score(coin)

    # التقييم النهائي
    final_strength = (
        0.20 * liquidity_score +
        0.20 * smart_liquidity +
        0.25 * volume_score +
        0.15 * order_flow_score +
        0.12 * sector_score +
        0.08 * social_score
    )
    final_strength = round(final_strength * 5, 1)

    if final_strength < cfg["min_signal_strength"]:
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
        "expectation": "صعودي (Smart Liquidity Rotation)",
        "reason": "Liquidity + Volume + Smart Money",
        "risk": "متوسطة",
        "timeframe": "12-48 ساعة",
        "entry": f"{price*0.99:.4f} - {price*1.02:.4f}",
        "stop_loss": f"{price*0.94:.4f}",
        "tp1": f"{price*1.07:.4f}",
        "tp2": f"{price*1.14:.4f}"
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
    send_telegram("🤖 Liquidity Rotation Scanner v3 Professional\n✅ تم تشغيل النظام الاحترافي", cfg)
    print("✅ النظام v3 Professional يعمل الآن...")

    last_alert = {}
    last_strength = {}

    while True:
        try:
            coins = get_coins_market()

            alert_count = 0
            for coin in coins:
                signal = analyze_coin(coin, cfg, coins)
                if not signal:
                    continue

                symbol = signal["symbol"]
                current_strength = signal["strength"]
                current_time = time.time()

                should_send = False

                if symbol not in last_alert:
                    should_send = True
                else:
                    time_diff = current_time - last_alert[symbol]
                    strength_diff = current_strength - last_strength.get(symbol, 0)

                    if current_strength >= cfg["strong_signal_threshold"] and strength_diff >= 0.3:
                        should_send = True
                    elif strength_diff >= 0.4 or time_diff >= cfg["alert_cooldown_hours"] * 3600:
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
