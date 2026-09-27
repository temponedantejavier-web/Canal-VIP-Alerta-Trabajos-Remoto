import os
import json
import time
import threading
from datetime import datetime, timedelta
import requests
import feedparser
from bs4 import BeautifulSoup
from openai import OpenAI
from flask import Flask, request, jsonify

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_VIP_CHANNEL_ID = os.getenv("TELEGRAM_VIP_CHANNEL_ID", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
SUBSCRIPTION_PRICE = float(os.getenv("SUBSCRIPTION_PRICE", "5000.0"))

POSTED_JOBS_FILE = "posted_jobs.json"
SUBSCRIBERS_FILE = "subscribers.json"
SCAN_INTERVAL_SECONDS = 1800

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def load_json_file(filename, default_value):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_value

def save_json_file(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def send_telegram_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.json()
    except Exception as e:
        print(f"Error Telegram: {e}")
        return None

def create_one_time_invite_link(channel_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/createChatInviteLink"
    payload = {
        "chat_id": channel_id,
        "member_limit": 1,
        "name": "Acceso VIP"
    }
    try:
        res = requests.post(url, json=payload, timeout=10).json()
        if res.get("ok"):
            return res["result"]["invite_link"]
    except Exception as e:
        print(f"Error Link: {e}")
    return None

def kick_user_from_channel(channel_id, user_id):
    ban_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/banChatMember"
    unban_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/unbanChatMember"
    try:
        requests.post(ban_url, json={"chat_id": channel_id, "user_id": user_id}, timeout=10)
        requests.post(unban_url, json={"chat_id": channel_id, "user_id": user_id, "only_if_banned": True}, timeout=10)
    except Exception as e:
        print(f"Error Kick: {e}")

def create_mp_preference(user_id):
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "items": [
            {
                "title": "Suscripcion VIP 30 dias",
                "quantity": 1,
                "unit_price": SUBSCRIPTION_PRICE,
                "currency_id": "ARS"
            }
        ],
        "back_urls": {"success": f"https://t.me/{BOT_USERNAME}"},
        "auto_return": "approved",
        "notification_url": f"{WEBHOOK_URL}/webhook/mercadopago",
        "external_reference": str(user_id)
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=15).json()
        return res.get("init_point")
    except Exception as e:
        print(f"Error MP: {e}")
        return None

@app.route("/webhook/mercadopago", methods=["POST"])
def mercadopago_webhook():
    data = request.get_json() or {}
    payment_id = data.get("data", {}).get("id") or request.args.get("data.id") or request.args.get("id")

    if not payment_id:
        return jsonify({"status": "ignored"}), 200

    payment_url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    try:
        payment_info = requests.get(payment_url, headers=headers, timeout=15).json()
        if payment_info.get("status") == "approved":
            user_id = payment_info.get("external_reference")
            if user_id:
                subscribers = load_json_file(SUBSCRIBERS_FILE, {})
                if str(user_id) in subscribers and subscribers[str(user_id)].get("last_payment_id") == str(payment_id):
                    return jsonify({"status": "already_processed"}), 200

                expiration_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
                subscribers[str(user_id)] = {
                    "user_id": user_id,
                    "expiration_date": expiration_date,
                    "last_payment_id": str(payment_id),
                    "status": "active"
                }
                save_json_file(SUBSCRIBERS_FILE, subscribers)
                invite_link = create_one_time_invite_link(TELEGRAM_VIP_CHANNEL_ID)
                msg = f"🎉 *PAGO CONFIRMADO*\n\nAcceso VIP activo hasta: `{expiration_date}`\n\n🔗 Link: {invite_link}" if invite_link else "🎉 Pago confirmado. Contacta soporte."
                send_telegram_message(user_id, msg)
    except Exception as e:
        print(f"Error Webhook: {e}")

    return jsonify({"status": "ok"}), 200

@app.route("/")
def health_check():
    return "Bot VIP Activo", 200

def generate_job_post_ai(title, description, raw_link):
    if not client:
        return f"💼 *{title}*\n\n📝 {description[:250]}...\n\n🔗 [Postularse]({raw_link})"

    prompt = f"Crea una publicacion para Telegram sobre el empleo: {title}\nDescripcion: {description}\nLink: {raw_link}"
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception:
        return f"💼 *{title}*\n\n🔗 [Ver vacante]({raw_link})"

def run_job_scraper():
    while True:
        posted_jobs = set(load_json_file(POSTED_JOBS_FILE, []))
        new_jobs = []

        try:
            feed = feedparser.parse("https://weworkremotely.com/remote-jobs.rss")
            for entry in feed.entries[:10]:
                job_id = entry.id if 'id' in entry else entry.link
                if job_id not in posted_jobs:
                    desc = BeautifulSoup(entry.summary, "html.parser").get_text() if hasattr(entry, 'summary') else ""
                    new_jobs.append((job_id, entry.title, desc, entry.link))
        except Exception:
            pass

        published_count = 0
        for job_id, title, description, link in new_jobs:
            if published_count >= 3:
                break
            post_content = generate_job_post_ai(title, description, link)
            res = send_telegram_message(TELEGRAM_VIP_CHANNEL_ID, post_content)
            if res and res.get("ok"):
                posted_jobs.add(job_id)
                published_count += 1
                time.sleep(5)

        save_json_file(POSTED_JOBS_FILE, list(posted_jobs))
        time.sleep(SCAN_INTERVAL_SECONDS)

def run_telegram_listener():
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"timeout": 20, "offset": offset}
            response = requests.get(url, params=params, timeout=25).json()
            for update in response.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if message and "text" in message:
                    chat_id = message["chat"]["id"]
                    text = message["text"].strip().lower()

                    if text in ["/start", "/suscribirse", "suscribirme"]:
                        pay_link = create_mp_preference(chat_id)
                        msg = f"🚀 *BIENVENIDO AL BOT VIP*\n\nPrecio: ${SUBSCRIPTION_PRICE:,.0f} ARS / mes."
                        reply_markup = {"inline_keyboard": [[{"text": "💳 PAGAR SUSCRIPCION", "url": pay_link}]]} if pay_link else None
                        send_telegram_message(chat_id, msg, reply_markup=reply_markup)

                    elif text in ["/estado", "/mi_estado"]:
                        subscribers = load_json_file(SUBSCRIBERS_FILE, {})
                        sub = subscribers.get(str(chat_id))
                        if sub and sub.get("status") == "active":
                            send_telegram_message(chat_id, f"✅ Activo hasta: `{sub.get('expiration_date')}`")
                        else:
                            send_telegram_message(chat_id, "❌ Sin suscripcion activa.")
        except Exception:
            pass
        time.sleep(1)

def run_expiration_checker():
    while True:
        subscribers = load_json_file(SUBSCRIBERS_FILE, {})
        now = datetime.now()
        updated = False
        for user_id, data in list(subscribers.items()):
            if data.get("status") == "active":
                exp_date = datetime.strptime(data["expiration_date"], "%Y-%m-%d %H:%M:%S")
                if now > exp_date:
                    kick_user_from_channel(TELEGRAM_VIP_CHANNEL_ID, user_id)
                    send_telegram_message(user_id, "🔴 *TU SUSCRIPCION HA VENCIDO*")
                    data["status"] = "expired"
                    updated = True
        if updated:
            save_json_file(SUBSCRIBERS_FILE, subscribers)
        time.sleep(43200)

if __name__ == "__main__":
    threading.Thread(target=run_telegram_listener, daemon=True).start()
    threading.Thread(target=run_job_scraper, daemon=True).start()
    threading.Thread(target=run_expiration_checker, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
