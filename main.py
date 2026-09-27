import os
import json
import time
import threading
import sys
from datetime import datetime, timedelta
import requests
import feedparser
from bs4 import BeautifulSoup
from openai import OpenAI
from flask import Flask, request, jsonify

sys.stdout.reconfigure(line_buffering=True)

app = Flask(__name__)

# ------------------------------------------------------------------------------
# CONFIGURACIÓN DE VARIABLES
# ------------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

TELEGRAM_VIP_CHANNEL_ID = (
    os.getenv("TELEGRAM_VIP_CHANNEL_ID") or 
    os.getenv("Telegram_vip_channel") or ""
).strip()

BOT_USERNAME = (
    os.getenv("BOT_USERNAME") or 
    os.getenv("Bot_username") or ""
).strip().lstrip("@")

OPENAI_API_KEY = (
    os.getenv("OPENAI_API_KEY") or 
    os.getenv("OpenAI_API_Key") or ""
).strip()

MP_ACCESS_TOKEN = (
    os.getenv("MP_ACCESS_TOKEN") or 
    os.getenv("Mp_acces_token") or 
    os.getenv("Mp_access_token") or ""
).strip()

MP_FIXED_LINK = (
    os.getenv("MP_FIXED_LINK") or 
    os.getenv("Mp_fixed_link") or 
    os.getenv("MP_LINK") or ""
).strip()

# Agregá tu Telegram Chat ID personal en Render si querés restringir /activar solo a vos
ADMIN_CHAT_ID = (os.getenv("ADMIN_CHAT_ID") or "").strip()

raw_price = (
    os.getenv("SUBSCRIPTION_PRICE") or 
    os.getenv("Suscription_price") or "5000"
)
try:
    SUBSCRIPTION_PRICE = float(str(raw_price).replace(",", "").strip())
except Exception:
    SUBSCRIPTION_PRICE = 5000.0

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
        print(f"❌ Error al guardar {filename}: {e}", flush=True)

# ------------------------------------------------------------------------------
# TELEGRAM HELPERS
# ------------------------------------------------------------------------------
def send_telegram_message(chat_id, text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return None

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
        print(f"❌ Excepcion enviando mensaje Telegram: {e}", flush=True)
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
        print(f"❌ Excepcion creando link de invitacion: {e}", flush=True)
    return None

def kick_user_from_channel(channel_id, user_id):
    ban_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/banChatMember"
    unban_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/unbanChatMember"
    try:
        requests.post(ban_url, json={"chat_id": channel_id, "user_id": user_id}, timeout=10)
        requests.post(unban_url, json={"chat_id": channel_id, "user_id": user_id, "only_if_banned": True}, timeout=10)
        print(f"🚫 Usuario {user_id} removido del canal VIP.", flush=True)
    except Exception as e:
        print(f"❌ Error al remover usuario {user_id}: {e}", flush=True)

def activate_subscriber(user_id):
    subscribers = load_json_file(SUBSCRIBERS_FILE, {})
    expiration_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    subscribers[str(user_id)] = {
        "user_id": str(user_id),
        "expiration_date": expiration_date,
        "status": "active"
    }
    save_json_file(SUBSCRIBERS_FILE, subscribers)
    invite_link = create_one_time_invite_link(TELEGRAM_VIP_CHANNEL_ID)
    
    if invite_link:
        msg = (
            f"🎉 *¡PAGO CONFIRMADO Y SUSCRIPCIÓN ACTIVADA!*\n\n"
            f"Tu acceso VIP está activo hasta el: `{expiration_date}`\n\n"
            f"🔗 *Tu enlace exclusivo de ingreso al Canal VIP:*\n{invite_link}\n\n"
            f"⚠️ _Nota: Este enlace es personal y de un solo uso._"
        )
    else:
        msg = "🎉 *¡Suscripción activada!* Por favor contactá al administrador para recibir tu enlace."

    send_telegram_message(user_id, msg)
    return expiration_date

@app.route("/")
def health_check():
    return "Bot VIP de Empleos Activo 24/7", 200

# ------------------------------------------------------------------------------
# ESCÁNER DE EMPLEOS E IA
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
        print(f"⚠️ Error OpenAI: {e}", flush=True)
        return f"💼 *{title}*\n\n🔗 [Postularse aqui]({raw_link})"

def run_job_scraper():
    print("🚀 Hilo iniciado: Escaner de Empleos", flush=True)
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
            print(f"❌ Error Scraper WWR: {e}", flush=True)

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
    print("🎧 Hilo iniciado: Bot Listener de Telegram", flush=True)
    
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
                time.sleep(5)
                continue

            for update in response.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if message and "text" in message:
                    chat_id = message["chat"]["id"]
                    text = message["text"].strip()

                    # COMANDO /START O /SUSCRIBIRSE
                    if text.lower() in ["/start", "/suscribirse", "suscribirme"]:
                        msg = (
                            f"🚀 *BIENVENIDO AL BOT DE EMPLEOS REMOTOS VIP*\n\n"
                            f"Accedé a publicaciones diarias con vacantes 100% remotas pagadas en USD.\n\n"
                            f"💰 *Precio Suscripción:* ${SUBSCRIPTION_PRICE:,.0f} ARS / mes.\n\n"
                            f"📌 *Tu ID de Usuario:* `{chat_id}`"
                        )
                        reply_markup = {
                            "inline_keyboard": [
                                [{"text": f"💳 PAGAR SUSCRIPCIÓN (${SUBSCRIPTION_PRICE:,.0f} ARS)", "url": MP_FIXED_LINK}]
                            ]
                        }
                        send_telegram_message(chat_id, msg, reply_markup=reply_markup)

                    # COMANDO ADMINISTRADOR /ACTIVAR ID
                    elif text.startswith("/activar"):
                        parts = text.split()
                        if len(parts) > 1:
                            target_id = parts[1]
                            exp = activate_subscriber(target_id)
                            send_telegram_message(chat_id, f"✅ Usuario `{target_id}` activado con éxito hasta `{exp}`.")
                        else:
                            send_telegram_message(chat_id, "⚠️ Uso correcto: `/activar ID_DEL_USUARIO`")

                    # COMANDO /ESTADO
                    elif text.lower() in ["/estado", "/mi_estado"]:
                        subscribers = load_json_file(SUBSCRIBERS_FILE, {})
                        sub = subscribers.get(str(chat_id))
                        if sub and sub.get("status") == "active":
                            send_telegram_message(chat_id, f"✅ Tu suscripción está *ACTIVA* hasta: `{sub.get('expiration_date')}`")
                        else:
                            send_telegram_message(chat_id, "❌ No tenés una suscripción activa. Usá /suscribirse para abonar tu acceso.")
        except Exception as e:
            print(f"❌ Error en Listener Telegram: {e}", flush=True)
            time.sleep(5)

def run_expiration_checker():
    print("🕒 Hilo iniciado: Verificador de Vencimientos", flush=True)
    while True:
        subscribers = load_json_file(SUBSCRIBERS_FILE, {})
        now = datetime.now()
        updated = False
        for user_id, data in list(subscribers.items()):
            if data.get("status") == "active":
                exp_date = datetime.strptime(data["expiration_date"], "%Y-%m-%d %H:%M:%S")
                if now > exp_date:
                    kick_user_from_channel(TELEGRAM_VIP_CHANNEL_ID, user_id)
                    send_telegram_message(user_id, "🔴 *TU SUSCRIPCIÓN VIP HA VENCIDO*\n\nUsá /suscribirse para renovar tu acceso.")
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
