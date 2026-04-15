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

# ====================== Altcoin Strength Filter (معدل - أوزان متساوية) ======================
def get_altcoin_strength(coins):
    try:
        # فلتر العملات حسب الماركت كاب أولاً
        filtered_coins = [c for c in coins if 
                         c.get("market_cap") and 
                         CONFIG_FILE["market_cap_min"] <= c.get("market_cap", 0) <= CONFIG_FILE["market_cap_max"]]

        if len(filtered_coins) < 20:
            return {"strength": 0.5, "status": "Neutral", "filtered_count": len(filtered_coins)}

        # تقسيم المجموعات
        g1 = filtered_coins[0:10]      # أقوى 10 عملات (بعد BTC)
        g2 = filtered_coins[10:50]     # 11-50
        g3 = filtered_coins[50:100]    # 51-100
        g4 = filtered_coins[100:]      # 101 فما فوق (Small & Mid)

        def group_score(group):
            if not group:
                return 0.5
            positive = sum(1 for c in group if c.get("price_change_percentage_24h", 0) > 0)
            avg_change = sum(c.get("price_change_percentage_24h", 0) for c in group) / len(group)
            return (positive / len(group)) * 0.6 + (1 if avg_change > 2 else 0.7 if avg_change > 0 else 0.2)

        score1 = group_score(g1)
        score2 = group_score(g2)
        score3 = group_score(g3)
        score4 = group_score(g4)

        final_strength = (score1 + score2 + score3 + score4) / 4
        final_strength = round(min(max(final_strength, 0.0), 1.0), 2)

        if final_strength >= 0.75:
            status = "Strong"
        elif final_strength >= 0.55:
            status = "Moderate"
        elif final_strength >= 0.40:
            status = "Neutral"
        else:
            status = "Weak"

        print(f"🌍 Altcoin Strength: {status} | Score: {final_strength} | Filtered: {len(filtered_coins)} عملة")

        return {
            "strength": final_strength, 
            "status": status, 
            "filtered_count": len(filtered_coins)
        }

    except Exception as e:
        print(f"⚠️ خطأ في Altcoin Strength: {e}")
        return {"strength": 0.55, "status": "Neutral", "filtered_count": 0}

# ====================== التحليل الرئيسي ======================
def analyze_coin(coin, cfg, alt_strength):
    symbol = coin.get("symbol", "").upper()
    name = coin.get("name", "")
    price = coin.get("current_price", 0)
    market_cap = coin.get("market_cap", 0) or 0
    volume_24h = coin.get("total_volume", 0) or 0

    if not (cfg["market_cap_min"] <= market_cap <= cfg["market_cap_max"]):
        return None

    liquidity_score = 0.85
    volume_score = 0.7 if volume_24h > 60000000 else 0.45

    base_strength = (liquidity_score + volume_score) / 2 * 5
    final_strength = round(min(base_strength * alt_strength["strength"], 5.0), 1)

    if final_strength < cfg["min_signal_strength"] and final_strength < cfg["strong_signal_threshold"]:
        return None

    confidence = "High" if final_strength >= 4.3 else "Medium" if final_strength >= 3.8 else "Low"

    signal = {
        "symbol": symbol,
        "name": name,
        "price": price,
        "market_cap": market_cap,
        "strength": final_strength,
        "confidence": confidence,
        "direction": "Long",
        "expectation": "صعودي (Altcoin Rotation)",
        "sector": "غير محدد",
        "reason": f"Alt Strength: {alt_strength['status']} (Score: {alt_strength['strength']})",
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
    send_telegram("🤖 Liquidity Rotation Scanner v2.7\n✅ تم تشغيل النظام مع Altcoin Strength Filter", cfg)
    print("✅ النظام v2.7 يعمل الآن مع Altcoin Strength Filter...")

    last_alert = {}
    last_strength = {}

    while True:
        try:
            alt_strength = get_altcoin_strength(get_coins_market())
            coins = get_coins_market()

            print(f"جاري تحليل {len(coins)} عملة (بعد فلتر الماركت كاب: {alt_strength.get('filtered_count', 0)})")

            alert_count = 0
            for coin in coins:
                signal = analyze_coin(coin, cfg, alt_strength)
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

                time.sleep(0.5)

            print(f"✅ انتهى الفحص - تم إرسال {alert_count} تنبيه")

        except Exception as e:
            print(f"❌ خطأ عام: {e}")
            time.sleep(60)

        time.sleep(cfg.get("check_interval", 300))

if __name__ == "__main__":
    main()
