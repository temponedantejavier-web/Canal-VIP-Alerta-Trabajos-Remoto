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

# ------------------------------------------------------------------------------
# CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ------------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_VIP_CHANNEL_ID = os.getenv("TELEGRAM_VIP_CHANNEL_ID", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
SUBSCRIPTION_PRICE = float(os.getenv("SUBSCRIPTION_PRICE", "5000.0"))

POSTED_JOBS_FILE = "posted_jobs.json"
SUBSCRIBERS_FILE = "subscribers.json"
SCAN_INTERVAL_SECONDS = 1800

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ------------------------------------------------------------------------------
# MANEJO DE ARCHIVOS JSON
# ------------------------------------------------------------------------------
def load_json_file(filename, default_value):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_value

def save_json_file(filename, data):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"❌ Error al guardar {filename}: {e}")

# ------------------------------------------------------------------------------
# TELEGRAM HELPERS
# ------------------------------------------------------------------------------
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
        print(f"❌ Error al enviar mensaje por Telegram: {e}")
        return None

def create_one_time_invite_link(channel_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/createChatInviteLink"
    payload = {
        "chat_id": channel_id,
        "member_limit": 1,
        "name": "Acceso VIP Suscripcion"
    }
    try:
        res = requests.post(url, json=payload, timeout=10).json()
        if res.get("ok"):
            return res["result"]["invite_link"]
    except Exception as e:
        print(f"❌ Error al crear link de invitacion: {e}")
    return None

def kick_user_from_channel(channel_id, user_id):
    ban_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/banChatMember"
    unban_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/unbanChatMember"
    try:
        requests.post(ban_url, json={"chat_id": channel_id, "user_id": user_id}, timeout=10)
        requests.post(unban_url, json={"chat_id": channel_id, "user_id": user_id, "only_if_banned": True}, timeout=10)
        print(f"🚫 Usuario {user_id} removido del canal VIP.")
    except Exception as e:
        print(f"❌ Error al remover usuario {user_id}: {e}")

# ------------------------------------------------------------------------------
# MERCADO PAGO Y WEBHOOKS
# ------------------------------------------------------------------------------
def create_mp_preference(user_id):
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "items": [
            {
                "title": "Suscripcion VIP 30 dias - Alertas Empleos USD",
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
        res = requests.post(url, json=payload, headers=headers, timeout=15)
        res_data = res.json()
        
        if "init_point" in res_data:
            return res_data["init_point"]
        else:
            print(f"❌ Respuesta rechazada por MercadoPago: {res_data}")
            return None
    except Exception as e:
        print(f"❌ Error de conexion con MercadoPago: {e}")
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
                
                if invite_link:
                    msg = (
                        f"🎉 *¡PAGO CONFIRMADO EXITOSAMENTE!*\n\n"
                        f"Tu suscripcion VIP esta activa hasta el: `{expiration_date}`\n\n"
                        f"🔗 *Tu enlace exclusivo al Canal VIP:*\n{invite_link}\n\n"
                        f"⚠️ _Este enlace es de uso unico y personal._"
                    )
                else:
                    msg = "🎉 *¡Pago confirmado!* Contacta al soporte para recibir tu enlace."

                send_telegram_message(user_id, msg)
                print(f"✅ Suscripcion activada exitosamente para usuario {user_id}")
    except Exception as e:
        print(f"❌ Error procesando Webhook MP: {e}")

    return jsonify({"status": "ok"}), 200

@app.route("/")
def health_check():
    return "Bot VIP de Empleos Activo 24/7", 200

# ------------------------------------------------------------------------------
# CURADOR IA Y ESCÁNER DE EMPLEOS
# ------------------------------------------------------------------------------
def generate_job_post_ai(title, description, raw_link):
    if not client:
        return (
            f"💼 *{title}*\n\n"
            f"🌍 *Modalidad:* 100% Remoto / USD\n"
            f"📝 *Descripcion:* {description[:250]}...\n\n"
            f"🔗 [Postularse Directamente]({raw_link})"
        )

    prompt = f"""
    Eres el redactor del Canal VIP de Empleos Remotos USD.
    Crea una publicacion clara y profesional en Markdown para Telegram.

    Titulo: {title}
    Descripcion: {description}
    Link: {raw_link}

    Estructura requerida:
    💼 *Puesto:* [Nombre del puesto]
    🌍 *Modalidad:* 100% Remoto (USD)
    💡 *Resumen:* [1 o 2 oraciones clave]

    🔗 [POSTULARME AL PUESTO DE TRABAJO]({raw_link})
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"⚠️ Error OpenAI: {e}")
        return f"💼 *{title}*\n\n🔗 [Postularse aqui]({raw_link})"

def run_job_scraper():
    print("🚀 Hilo iniciado: Escaner de Empleos")
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
        except Exception as e:
            print(f"❌ Error Scraper WWR: {e}")

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

# ------------------------------------------------------------------------------
# LISTENER Y CONTROL DE VENCIMIENTOS
# ------------------------------------------------------------------------------
def run_telegram_listener():
    print("🎧 Hilo iniciado: Bot Listener de Telegram")
    
    # Eliminar cualquier webhook previo para permitir Long-Polling
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook", timeout=5)
    except Exception:
        pass

    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"timeout": 20, "offset": offset}
            response = requests.get(url, params=params, timeout=25).json()

            if not response.get("ok"):
                error_code = response.get("error_code")
                if error_code == 409:
                    print("⚠️ Conflicto 409: Otra instancia intentando conectarse. Reintentando en 10s...")
                    time.sleep(10)
                    continue
                time.sleep(5)
                continue

            for update in response.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if message and "text" in message:
                    chat_id = message["chat"]["id"]
                    text = message["text"].strip().lower()

                    if text in ["/start", "/suscribirse", "suscribirme"]:
                        pay_link = create_mp_preference(chat_id)
                        msg = (
                            f"🚀 *BIENVENIDO AL BOT DE EMPLEOS REMOTOS VIP*\n\n"
                            f"Accede a publicaciones diarias con vacantes 100% remotas pagadas en USD.\n\n"
                            f"💰 *Precio Suscripcion:* ${SUBSCRIPTION_PRICE:,.0f} ARS / mes."
                        )
                        reply_markup = None
                        if pay_link:
                            reply_markup = {
                                "inline_keyboard": [
                                    [{"text": f"💳 PAGAR SUSCRIPCION (${SUBSCRIPTION_PRICE:,.0f} ARS)", "url": pay_link}]
                                ]
                            }
                        send_telegram_message(chat_id, msg, reply_markup=reply_markup)

                    elif text in ["/estado", "/mi_estado"]:
                        subscribers = load_json_file(SUBSCRIBERS_FILE, {})
                        sub = subscribers.get(str(chat_id))
                        if sub and sub.get("status") == "active":
                            send_telegram_message(chat_id, f"✅ Tu suscripcion esta *ACTIVA* hasta: `{sub.get('expiration_date')}`")
                        else:
                            send_telegram_message(chat_id, "❌ No tienes una suscripcion activa. Usa /suscribirse para ingresar.")
        except Exception as e:
            print(f"❌ Error en Listener Telegram: {e}")
            time.sleep(5)

def run_expiration_checker():
    print("🕒 Hilo iniciado: Verificador de Vencimientos")
    while True:
        subscribers = load_json_file(SUBSCRIBERS_FILE, {})
        now = datetime.now()
        updated = False
        for user_id, data in list(subscribers.items()):
            if data.get("status") == "active":
                exp_date = datetime.strptime(data["expiration_date"], "%Y-%m-%d %H:%M:%S")
                if now > exp_date:
                    kick_user_from_channel(TELEGRAM_VIP_CHANNEL_ID, user_id)
                    send_telegram_message(user_id, "🔴 *TU SUSCRIPCION VIP HA VENCIDO*\n\nUsa /suscribirse para renovar tu acceso.")
                    data["status"] = "expired"
                    updated = True
        if updated:
            save_json_file(SUBSCRIBERS_FILE, subscribers)
        time.sleep(43200)

# ------------------------------------------------------------------------------
# INICIALIZACIÓN
# ------------------------------------------------------------------------------
def start_background_threads():
    threading.Thread(target=run_telegram_listener, daemon=True).start()
    threading.Thread(target=run_job_scraper, daemon=True).start()
    threading.Thread(target=run_expiration_checker, daemon=True).start()

start_background_threads()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
