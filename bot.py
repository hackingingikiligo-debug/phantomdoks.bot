import os
import re
import json
import html
import base64
import traceback
import requests
import whois
import telebot
from flask import Flask, request
from groq import Groq

# ============================================================
# КОНФИГ
# ============================================================
BOT_TOKEN = os.environ.get('BOT_TOKEN', '9082313783:AAG0d10RC63ZTy66xV4nXlfcDp6QOgYH6Zk')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
SERPER_API_KEY = os.environ.get('SERPER_API_KEY')

if not BOT_TOKEN:
    raise ValueError("Не установлен BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

MAX_MSG = 4000

# ============================================================
# УТИЛИТЫ
# ============================================================

def escape_html(text: str) -> str:
    return html.escape(str(text), quote=False)

def command_args(message) -> str:
    """Извлекает аргументы команды, игнорируя @имя_бота"""
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""

def send_long(chat_id: int, text: str, parse_mode: str = None):
    """Безопасная отправка сообщений с защитой от разрыва HTML-тегов."""
    if not text:
        return

    # Обычный текст
    if parse_mode != "HTML":
        for i in range(0, len(text), MAX_MSG):
            chunk = text[i:i + MAX_MSG]
            try:
                bot.send_message(chat_id, chunk, disable_web_page_preview=True)
            except Exception as e:
                print(f"Ошибка отправки: {e}")
        return

    # HTML-режим
    if len(text) > MAX_MSG:
        # Если текст длинный, срезаем теги, чтобы не сломать разметку
        text = re.sub(r"<[^>]*>", "", text)
        text = html.unescape(text)
        for i in range(0, len(text), MAX_MSG):
            try:
                bot.send_message(chat_id, text[i:i + MAX_MSG], disable_web_page_preview=True)
            except Exception as e:
                print(f"Ошибка отправки plain: {e}")
        return

    try:
        bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        print(f"Ошибка HTML: {e}")
        plain = html.unescape(re.sub(r"<[^>]*>", "", text))
        try:
            bot.send_message(chat_id, plain, disable_web_page_preview=True)
        except Exception as e2:
            print(f"Ошибка fallback: {e2}")

# ============================================================
# GROQ: текст + vision
# ============================================================

def ask_groq_agent(prompt: str, context: str = "") -> str:
    if not groq_client:
        return "Ошибка: GROQ_API_KEY не настроен."
    full = f"{context}\n\nЗапрос: {prompt}" if context else prompt
    try:
        r = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Ты — OSINT-ассистент. Работаешь только с публично доступной информацией."},
                {"role": "user", "content": full},
            ],
            model="openai/gpt-oss-120b",
            temperature=0.2,
            max_tokens=2048,
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        print(f"Groq Error: {e}")
        return f"Ошибка Groq: {e}"

def analyze_image_with_qwen(image_bytes: bytes, query: str = "Опиши изображение.") -> str:
    if not groq_client:
        return "Ошибка: GROQ_API_KEY не настроен."
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    try:
        r = groq_client.chat.completions.create(
            model="qwen/qwen3.8-27b",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": query},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            temperature=0.2,
            max_tokens=1500,
        )
        return r.choices[0].message.content
    except Exception as e:
        print(f"Qwen Error: {e}")
        return f"Ошибка Qwen: {e}"

# ============================================================
# SERPER
# ============================================================

def search_google(query: str, num: int = 5) -> list:
    if not SERPER_API_KEY:
        return []
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            data=json.dumps({"q": query, "num": num}),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("organic", [])
    except Exception as e:
        print(f"Serper Error: {e}")
        return []

def results_to_plain(query: str, results: list) -> str:
    if not results:
        return f"--- {query}: пусто ---\n"
    out = f"--- {query} ---\n"
    for item in results:
        out += f"{item.get('title','')} | {item.get('link','')}\n{item.get('snippet','')}\n\n"
    return out

def results_to_html(query: str, results: list) -> str:
    if not results:
        return ""
    out = f"🔎 <b>{escape_html(query)}</b>\n"
    for item in results:
        title = escape_html(item.get("title", ""))
        snippet = escape_html(item.get("snippet", ""))
        url = html.escape(str(item.get("link", "")), quote=True)

        if not url.startswith(("https://", "http://")):
            out += f"• {title}\n{snippet}\n\n"
            continue
        out += f'• <a href="{url}">{title}</a>\n{snippet}\n\n'
    return out

# ============================================================
# ТЕЛЕФОН
# ============================================================

def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("8"):
        return "+7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    if len(digits) == 10:
        return "+7" + digits
    return "+" + digits if digits else phone

def phone_osint_queries(phone: str) -> list:
    d = re.sub(r"\D", "", phone)
    last10 = d[-10:] if len(d) >= 10 else d
    formats = list({phone, f"+{d}", f"8{last10}", f"7{last10}", d, last10})
    queries = []
    for f in formats:
        queries.append(f'"{f}"')
        queries.append(f'"{f}" (site:vk.com OR site:ok.ru OR site:avito.ru OR site:t.me)')
    queries += [f'"{phone}" объявления', f'"{phone}" whatsapp', f'"{phone}" telegram']
    seen, uniq = set(), []
    for q in queries:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq[:8]

# ============================================================
# КОМАНДЫ
# ============================================================

@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.reply_to(message,
        "👋 OSINT-бот (только открытые источники).\n\n"
        "/nick username — поиск по нику\n"
        "/domain example.com — WHOIS\n"
        "/phone +79001234567 — быстрые ссылки\n"
        "/phone_osint +79001234567 — глубокий поиск\n"
        "/doks <запрос> — каскадный поиск (можно фото с подписью /doks)\n\n"
        "Отправь фото — проанализирую через Qwen."
    )

@bot.message_handler(commands=["nick"])
def cmd_nick(message):
    nick = command_args(message)
    if not nick:
        bot.reply_to(message, "Напиши ник: /nick username")
        return
    sites = {
        "Telegram": f"https://t.me/{nick}",
        "GitHub": f"https://github.com/{nick}",
        "TikTok": f"https://tiktok.com/@{nick}",
        "Instagram": f"https://instagram.com/{nick}",
        "VK": f"https://vk.com/{nick}",
    }
    out = f"👤 <b>Ник {escape_html(nick)}:</b>\n\n"
    for site, url in sites.items():
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            out += f"{'✅' if r.status_code == 200 else '❌'} {site}: {url}\n"
        except Exception:
            out += f"⚠️ {site}: ошибка сети\n"
    send_long(message.chat.id, out, parse_mode="HTML")

@bot.message_handler(commands=["domain"])
def cmd_domain(message):
    domain = command_args(message)
    if not domain:
        bot.reply_to(message, "Напиши домен: /domain example.com")
        return
    try:
        w = whois.whois(domain)
        out = (
            f"🌐 <b>WHOIS {escape_html(domain)}</b>\n\n"
            f"👤 Владелец: {escape_html(w.name or 'Скрыто')}\n"
            f"📅 Регистрация: {escape_html(str(w.creation_date or 'Нет данных'))}\n"
            f"🏢 Организация: {escape_html(w.org or 'Скрыто')}\n"
            f"📧 Email: {escape_html(str(w.emails or 'Скрыто'))}\n"
        )
        send_long(message.chat.id, out, parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ WHOIS ошибка: {e}")

@bot.message_handler(commands=["phone"])
def cmd_phone(message):
    raw = command_args(message)
    if not raw:
        bot.reply_to(message, "Напиши: /phone +79001234567")
        return
    phone = normalize_phone(raw)
    d = re.sub(r"\D", "", phone)
    out = (
        f"📱 <b>{escape_html(phone)}</b>\n\n"
        f"🟢 WhatsApp: https://wa.me/{d}\n"
        f"🔵 Telegram: https://t.me/+{d}\n\n"
        f"🔎 Глубже: <b>/phone_osint {escape_html(phone)}</b>"
    )
    send_long(message.chat.id, out, parse_mode="HTML")

@bot.message_handler(commands=["phone_osint"])
def cmd_phone_osint(message):
    raw = command_args(message)
    if not raw:
        bot.reply_to(message, "Напиши: /phone_osint +79001234567")
        return
    phone = normalize_phone(raw)
    bot.reply_to(message, f"🕵️ Поиск упоминаний <b>{escape_html(phone)}</b>…", parse_mode="HTML")

    all_plain, all_html = "", ""
    found_any = False

    for q in phone_osint_queries(phone):
        r = search_google(q)
        if r:
            found_any = True
        all_plain += results_to_plain(q, r)
        all_html += results_to_html(q, r)

    if not found_any:
        bot.reply_to(message, "❌ Ничего не найдено.")
        return

    bot.reply_to(message, "🧠 Анализирую…")
    report = ask_groq_agent(
        prompt=f"Номер: {phone}. Публичные упоминания:\n{all_plain}\n\nСоставь OSINT-отчёт: имена/ники/компании, площадки, регион, пробелы."
    )
    send_long(message.chat.id, f"📋 ОТЧЁТ:\n\n{report}")
    send_long(message.chat.id, f"🔗 <b>Источники:</b>\n\n{all_html}", parse_mode="HTML")

# ============================================================
# /doks
# ============================================================

def process_doks(message, user_query: str, has_photo: bool):
    chat_id = message.chat.id
    image_context = ""

    if has_photo:
        bot.reply_to(message, "📸 Фото получено. Анализирую через Qwen…")
        try:
            file_id = message.photo[-1].file_id
            info = bot.get_file(file_id)
            data = bot.download_file(info.file_path)
            image_context = analyze_image_with_qwen(
                data,
                query="Опиши всё на фото: текст (OCR), лица, номера, адреса, вывески, логотипы."
            )
        except Exception as e:
            image_context = f"Ошибка анализа фото: {e}"

    if not user_query and not image_context:
        bot.reply_to(message, "Напиши запрос или приложи фото с подписью /doks.")
        return

    bot.reply_to(message, "🕵️ Этап 1: первичный поиск…")

    base = user_query or "фото"
    qraw = ask_groq_agent(
        f"Сгенерируй 5 поисковых запросов для открытой информации по теме: {base}. "
        f"Контекст из фото: {image_context[:400]}"
    )
    queries = [q.strip(" -•*0123456789.") for q in qraw.split("\n") if q.strip()][:5]

    all_plain = image_context + "\n\n" if image_context else ""
    all_html = (f"<b>📸 Анализ фото:</b>\n{escape_html(image_context)}\n\n" if image_context else "")
    
    found_any = False
    for q in queries:
        r = search_google(q)
        if r:
            found_any = True
        all_plain += results_to_plain(q, r)
        all_html += results_to_html(q, r)

    if not found_any and not image_context:
        bot.reply_to(message, "❌ Поиск не дал результатов.")
        return

    bot.reply_to(message, "🧠 Этап 2: углубление…")
    analysis = ask_groq_agent(
        f"Данные:\n{all_plain}\n\n"
        "Найди возможное имя/ник/компанию. Если нашёл — предложи 3 уточняющих запроса "
        "(только список, без пояснений). Если нет — ответь ровно «НЕТ ДАННЫХ»."
    )

    if "НЕТ ДАННЫХ" not in analysis and "Ошибка" not in analysis:
        deep = [q.strip(" -•*0123456789.") for q in analysis.split("\n") if q.strip()][:3]
        for q in deep:
            r = search_google(q)
            if r:
                found_any = True
            all_plain += results_to_plain(q, r)
            all_html += results_to_html(q, r)

    bot.reply_to(message, "🧠 Этап 3: финальный отчёт…")
    report = ask_groq_agent(
        prompt="Составь OSINT-отчёт по этим данным. Выдели находки и пробелы. Не выдумывай.",
        context=all_plain,
    )

    send_long(chat_id, f"📋 ОТЧЁТ:\n\n{report}")
    if all_html.strip():
        send_long(chat_id, f"🔗 <b>Источники:</b>\n\n{all_html}", parse_mode="HTML")

@bot.message_handler(commands=["doks"])
def cmd_doks(message):
    process_doks(message, command_args(message), has_photo=False)

# ============================================================
# ФОТО
# ============================================================

@bot.message_handler(content_types=["photo"])
def cmd_photo(message):
    caption = (message.caption or "").strip()
    if caption.startswith("/doks"):
        # Извлекаем текст из подписи (например, "/doks тест" -> "тест")
        query = caption.replace("/doks", "").strip()
        process_doks(message, query, has_photo=True)
        return

    bot.reply_to(message, "📸 Анализирую через Qwen…")
    try:
        file_id = message.photo[-1].file_id
        info = bot.get_file(file_id)
        data = bot.download_file(info.file_path)
        result = analyze_image_with_qwen(
            data,
            query="Опиши подробно изображение. Распознай текст (OCR). Перечисли наблюдаемые признаки."
        )
        send_long(message.chat.id, result)
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка фото: {e}")

# ============================================================
# ВЕБХУК ДЛЯ RENDER
# ============================================================

@app.route("/", methods=["GET"])
def index():
    return "Bot is alive!"

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        if request.mimetype == "application/json":
            json_string = request.get_data().decode("utf-8")
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return "", 200
        return "Forbidden", 403
    except Exception as e:
        print(f"ОШИБКА ОБРАБОТКИ ВЕБХУКА: {e}")
        traceback.print_exc()
        return "Error", 500

@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return "RENDER_EXTERNAL_URL не установлен", 400
    bot.remove_webhook()
    success = bot.set_webhook(url=f"{url}/{BOT_TOKEN}")
    if success:
        return f"✅ Webhook успешно установлен: {url}/{BOT_TOKEN}", 200
    else:
        return "❌ Ошибка установки вебхука", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
