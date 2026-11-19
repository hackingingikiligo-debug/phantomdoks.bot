import os
import re
import json
import html
import base64
import threading
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

print("=== СТАРТ БОТА ===")
print(f"BOT_TOKEN задан: {bool(BOT_TOKEN)}")
print(f"GROQ_API_KEY задан: {bool(GROQ_API_KEY)}")
print(f"SERPER_API_KEY задан: {bool(SERPER_API_KEY)}")

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

def send_long(chat_id: int, text: str, parse_mode: str = None):
    if not text:
        return
    for i in range(0, len(text), MAX_MSG):
        chunk = text[i:i + MAX_MSG]
        try:
            bot.send_message(chat_id, chunk, parse_mode=parse_mode, disable_web_page_preview=True)
        except Exception as e:
            print(f"Ошибка HTML: {e}")
            try:
                bot.send_message(chat_id, chunk, disable_web_page_preview=True)
            except Exception as e2:
                print(f"Ошибка plain: {e2}")

# ============================================================
# GROQ / SERPER
# ============================================================
def ask_groq_agent(prompt: str, context: str = "") -> str:
    if not groq_client:
        return "Ошибка: GROQ_API_KEY не настроен."
    full = f"{context}\n\nЗапрос: {prompt}" if context else prompt
    try:
        r = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Ты — OSINT-ассистент."},
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
        out += f'• <a href="{item.get("link","")}">{escape_html(item.get("title",""))}</a>\n{escape_html(item.get("snippet",""))}\n\n'
    return out

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
    print(f"!!! /start от {message.chat.id} !!!")
    # Отправляем диагностику
    diag = (
        f"✅ Бот живой.\n\n"
        f"GROQ_API_KEY: {'✅ есть' if GROQ_API_KEY else '❌ НЕТ'}\n"
        f"SERPER_API_KEY: {'✅ есть' if SERPER_API_KEY else '❌ НЕТ'}\n"
        f"Groq-клиент: {'✅ создан' if groq_client else '❌ не создан'}\n"
        f"Модель текста: openai/gpt-oss-120b\n"
        f"Модель фото: qwen/qwen3.8-27b\n\n"
        f"Команды:\n"
        f"/nick username — поиск по нику\n"
        f"/domain example.com — WHOIS\n"
        f"/phone +79001234567 — ссылки\n"
        f"/phone_osint +79001234567 — глубокий поиск\n"
        f"/doks <запрос> — каскадный поиск (можно фото с подписью /doks)\n"
        f"/test_groq — проверить Groq\n"
        f"/test_serper — проверить Serper\n"
        f"/test_photo — инструкция по фото\n\n"
        f"Отправь фото — проанализирую через Qwen."
    )
    bot.reply_to(message, diag)

@bot.message_handler(commands=["test_groq"])
def cmd_test_groq(message):
    bot.reply_to(message, "Тестирую Groq...")
    result = ask_groq_agent("Скажи ровно одно слово: РАБОТАЕТ")
    bot.reply_to(message, f"Ответ Groq:\n{result}")

@bot.message_handler(commands=["test_serper"])
def cmd_test_serper(message):
    bot.reply_to(message, "Тестирую Serper...")
    r = search_google("test query", num=1)
    if r:
        bot.reply_to(message, f"Serper ✅ Найдено {len(r)} результатов")
    else:
        bot.reply_to(message, "Serper ❌ 0 результатов (ключ неверный или кончился лимит)")

@bot.message_handler(commands=["test_photo"])
def cmd_test_photo(message):
    bot.reply_to(message, "Отправь фото с подписью /doks или без — проанализирую.")

@bot.message_handler(commands=["nick"])
def cmd_nick(message):
    nick = message.text.replace("/nick", "").strip()
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
            out += f"⚠️ {site}: ошибка\n"
    send_long(message.chat.id, out, parse_mode="HTML")

@bot.message_handler(commands=["domain"])
def cmd_domain(message):
    domain = message.text.replace("/domain", "").strip()
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
        bot.reply_to(message, f"❌ WHOIS: {e}")

@bot.message_handler(commands=["phone"])
def cmd_phone(message):
    raw = message.text.replace("/phone", "").strip()
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
    raw = message.text.replace("/phone_osint", "").strip()
    if not raw:
        bot.reply_to(message, "Напиши: /phone_osint +79001234567")
        return
    phone = normalize_phone(raw)
    bot.reply_to(message, f"🕵️ Поиск упоминаний <b>{escape_html(phone)}</b>…", parse_mode="HTML")
    all_plain, all_html = "", ""
    for q in phone_osint_queries(phone):
        r = search_google(q)
        all_plain += results_to_plain(q, r)
        all_html += results_to_html(q, r)
    if not all_plain.strip():
        bot.reply_to(message, "❌ Ничего не найдено.")
        return
    bot.reply_to(message, "🧠 Анализирую…")
    report = ask_groq_agent(
        prompt=f"Номер: {phone}. Публичные упоминания:\n{all_plain}\n\nСоставь OSINT-отчёт: имена/ники/компании, площадки, регион, пробелы."
    )
    send_long(message.chat.id, f"📋 ОТЧЁТ:\n\n{report}")
    send_long(message.chat.id, f"🔗 <b>Источники:</b>\n\n{all_html}", parse_mode="HTML")

# /doks
def process_doks(message, user_query: str, has_photo: bool):
    chat_id = message.chat.id
    image_context = ""
    if has_photo:
        bot.reply_to(message, "📸 Анализирую фото…")
        try:
            file_id = message.photo[-1].file_id
            info = bot.get_file(file_id)
            data = bot.download_file(info.file_path)
            image_context = analyze_image_with_qwen(data, query="Опиши всё: OCR, лица, номера, адреса, вывески.")
        except Exception as e:
            image_context = f"Ошибка фото: {e}"
    if not user_query and not image_context:
        bot.reply_to(message, "Напиши запрос или приложи фото с подписью /doks.")
        return
    bot.reply_to(message, "🕵️ Этап 1: первичный поиск…")
    base = user_query or "фото"
    qraw = ask_groq_agent(f"Сгенерируй 5 поисковых запросов по: {base}. Контекст фото: {image_context[:400]}")
    queries = [q.strip(" -•*0123456789.") for q in qraw.split("\n") if q.strip()][:5]
    all_plain = image_context + "\n\n" if image_context else ""
    all_html = (f"<b>📸 Фото:</b>\n{escape_html(image_context)}\n\n" if image_context else "")
    for q in queries:
        r = search_google(q)
        all_plain += results_to_plain(q, r)
        all_html += results_to_html(q, r)
    if len(all_plain.strip()) < 50:
        bot.reply_to(message, "❌ Поиск пуст.")
        return
    bot.reply_to(message, "🧠 Этап 2: углубление…")
    analysis = ask_groq_agent(f"Данные:\n{all_plain}\n\nНайди имя/ник. Если есть — 3 уточняющих запроса. Если нет — «НЕТ ДАННЫХ».")
    if "НЕТ ДАННЫХ" not in analysis and "Ошибка" not in analysis:
        deep = [q.strip(" -•*0123456789.") for q in analysis.split("\n") if q.strip()][:3]
        for q in deep:
            r = search_google(q)
            all_plain += results_to_plain(q, r)
            all_html += results_to_html(q, r)
    bot.reply_to(message, "🧠 Этап 3: отчёт…")
    report = ask_groq_agent(prompt="Составь OSINT-отчёт по данным. Не выдумывай.", context=all_plain)
    send_long(chat_id, f"📋 ОТЧЁТ:\n\n{report}")
    if all_html.strip():
        send_long(chat_id, f"🔗 <b>Источники:</b>\n\n{all_html}", parse_mode="HTML")

@bot.message_handler(commands=["doks"])
def cmd_doks(message):
    process_doks(message, message.text.replace("/doks", "").strip(), has_photo=False)

@bot.message_handler(content_types=["photo"])
def cmd_photo(message):
    caption = (message.caption or "").strip()
    if caption.startswith("/doks"):
        process_doks(message, caption.replace("/doks", "").strip(), has_photo=True)
        return
    bot.reply_to(message, "📸 Анализирую…")
    try:
        file_id = message.photo[-1].file_id
        info = bot.get_file(file_id)
        data = bot.download_file(info.file_path)
        result = analyze_image_with_qwen(data, query="Опиши всё. OCR, лица, номера, адреса, вывески.")
        send_long(message.chat.id, result)
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка фото: {e}")

# ============================================================
# ВЕБХУК + ДИАГНОСТИКА
# ============================================================
@app.route("/", methods=["GET"])
def index():
    return "Bot is alive!"

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    print("!!! ПОЛУЧЕН WEBHOOK !!!")
    try:
        if request.mimetype == "application/json":
            json_string = request.get_data().decode("utf-8")
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return "", 200
        return "Forbidden", 403
    except Exception as e:
        print(f"ОШИБКА ОБРАБОТКИ: {e}")
        traceback.print_exc()
        return "Error", 500

@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return "RENDER_EXTERNAL_URL not set", 400
    bot.remove_webhook()
    success = bot.set_webhook(url=f"{url}/{BOT_TOKEN}")
    if success:
        return f"✅ Webhook установлен: {url}/{BOT_TOKEN}"
    return "❌ Ошибка установки", 500

@app.route("/webhook_info", methods=["GET"])
def webhook_info():
    return str(bot.get_webhook_info())

@app.route("/test", methods=["GET"])
def test_route():
    return f"OK. GROQ={bool(GROQ_API_KEY)}, SERPER={bool(SERPER_API_KEY)}"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
