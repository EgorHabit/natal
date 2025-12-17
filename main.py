import os
import re
from datetime import datetime
import httpx
import swisseph as swe
from fastapi import FastAPI, Request

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()

# Простое хранилище состояния (на бесплатных инстансах может сбрасываться при рестарте — для MVP ок)
SESSIONS = {}  # chat_id -> dict(state=..., data=...)


TOPIC_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "❤️ Отношения", "callback_data": "topic:relationships"},
        {"text": "💼 Работа", "callback_data": "topic:career"},
    ], [
        {"text": "💰 Деньги", "callback_data": "topic:money"},
        {"text": "🧠 Я и характер", "callback_data": "topic:self"},
    ], [
        {"text": "🔮 Общая", "callback_data": "topic:general"},
    ]]
}


def new_session():
    return {
        "state": "ASK_DATE",
        "data": {
            "date": None,
            "time": None,
            "city": None,
            "country": None,
            "tz": None,
            "lat": None,
            "lon": None,
            "topic": None
        }
    }


async def tg_send_message(chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(f"{TG_API}/sendMessage", json=payload)


async def tg_answer_callback(callback_query_id: str):
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(f"{TG_API}/answerCallbackQuery", json={"callback_query_id": callback_query_id})


async def set_webhook():
    if not PUBLIC_URL:
        return
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(f"{TG_API}/setWebhook", json={"url": f"{PUBLIC_URL}/webhook"})


def parse_date(s: str):
    # YYYY-MM-DD или DD.MM.YYYY
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return datetime.strptime(s, "%Y-%m-%d").date()
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", s):
        return datetime.strptime(s, "%d.%m.%Y").date()
    return None


def parse_time(s: str):
    # HH:MM (24h)
    s = s.strip()
    if re.fullmatch(r"\d{2}:\d{2}", s):
        h, m = map(int, s.split(":"))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return (h, m)
    return None


async def geocode_city(city: str, country: str):
    url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": "natal-bot/1.0 (contact: example@example.com)"}

    async def _try(q: str):
        params = {"q": q, "format": "json", "limit": 1}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
        if not data:
            return None
        return float(data[0]["lat"]), float(data[0]["lon"])

    # 1) city, country
    res = await _try(f"{city}, {country}")
    if res:
        return res

    # 2) city only
    res = await _try(city)
    if res:
        return res

    


def compute_chart(lat: float, lon: float, dt_local: datetime, tz_str: str):
    """
    Минимальный расчёт: планеты + Asc.
    Важно: для MVP просим пользователя ввести TZ правильно.
    """
    # Конвертируем локальное время в UTC через стандартную библиотеку zoneinfo (Python 3.9+)
    from zoneinfo import ZoneInfo
    dt_utc = dt_local.replace(tzinfo=ZoneInfo(tz_str)).astimezone(ZoneInfo("UTC"))

    # Julian day (UT)
    jd_ut = swe.julday(dt_utc.year, dt_utc.month, dt_utc.day,
                       dt_utc.hour + dt_utc.minute/60.0 + dt_utc.second/3600.0)

    # Планеты (геоцентрические, тропические)
    planets = {
        "Sun": swe.SUN,
        "Moon": swe.MOON,
        "Mercury": swe.MERCURY,
        "Venus": swe.VENUS,
        "Mars": swe.MARS,
        "Jupiter": swe.JUPITER,
        "Saturn": swe.SATURN,
        "Uranus": swe.URANUS,
        "Neptune": swe.NEPTUNE,
        "Pluto": swe.PLUTO
    }

    positions = {}
    for name, pid in planets.items():
        lonlat, _ = swe.calc_ut(jd_ut, pid)  # lonlat[0] = ecliptic longitude
        positions[name] = lonlat[0]

    # Дома/Asc
    # Placidus ("P") — норм для “обычной” западной астрологии
    houses, ascmc = swe.houses(jd_ut, lat, lon, b'P')
    asc = ascmc[0]  # Ascendant longitude

    return {
        "utc": dt_utc.isoformat(),
        "positions": positions,
        "asc": asc
    }


def deg_to_sign(deg: float):
    signs = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]
    idx = int(deg // 30) % 12
    within = deg % 30
    return signs[idx], within


def chart_to_text(chart: dict):
    lines = []
    # Asc
    s, within = deg_to_sign(chart["asc"])
    lines.append(f"Ascendant: {s} {within:.1f}°")

    for k in ["Sun","Moon","Mercury","Venus","Mars","Jupiter","Saturn","Uranus","Neptune","Pluto"]:
        s, within = deg_to_sign(chart["positions"][k])
        lines.append(f"{k}: {s} {within:.1f}°")
    return "\n".join(lines)


async def call_openai(system_prompt: str, user_text: str) -> str:
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "gpt-4.1-mini",
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        "max_output_tokens": 450
    }
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    out = []
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                out.append(c.get("text", ""))
    return "\n".join(out).strip() or "Не получилось сформировать ответ — попробуй написать иначе 🙂"


def topic_label(topic: str) -> str:
    return {
        "relationships": "отношения",
        "career": "работа/карьера",
        "money": "деньги",
        "self": "характер/личность",
        "general": "общая карта"
    }.get(topic, "общая тема")


@app.on_event("startup")
async def on_startup():
    await set_webhook()


@app.get("/")
async def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(req: Request):
    update = await req.json()

    # Callback (кнопки)
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        data = cq.get("data", "")
        await tg_answer_callback(cq["id"])

        sess = SESSIONS.get(chat_id) or new_session()
        SESSIONS[chat_id] = sess

        if data.startswith("topic:"):
            sess["data"]["topic"] = data.split(":", 1)[1]
            sess["state"] = "ASK_FREEFORM"
            await tg_send_message(chat_id,
                "Ок 🙂 Напиши одним сообщением, что именно хочешь разобрать по этой теме.\n"
                "Например: «почему у меня повторяются такие отношения?» или «куда расти в карьере?»"
            )
        return {"ok": True}

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    if not text:
        await tg_send_message(chat_id, "Напиши текстом 🙂")
        return {"ok": True}

    # Команды
    if text.lower() in ("/start", "start"):
        SESSIONS[chat_id] = new_session()
        await tg_send_message(chat_id,
            "Привет 🙂 Я помогу сделать натальную карту.\n\n"
            "Сначала введём данные.\n"
            "Введи дату рождения (YYYY-MM-DD или DD.MM.YYYY)."
        )
        return {"ok": True}

    if text.lower() in ("/reset", "reset"):
        SESSIONS[chat_id] = new_session()
        await tg_send_message(chat_id,
            "Сбросила ввод ✅\nВведи дату рождения (YYYY-MM-DD или DD.MM.YYYY)."
        )
        return {"ok": True}

    sess = SESSIONS.get(chat_id)
    if not sess:
        sess = new_session()
        SESSIONS[chat_id] = sess
        await tg_send_message(chat_id,
            "Давай начнём 🙂 Введи дату рождения (YYYY-MM-DD или DD.MM.YYYY)."
        )
        return {"ok": True}

    state = sess["state"]
    d = sess["data"]

    # Шаги ввода
    if state == "ASK_DATE":
        dt = parse_date(text)
        if not dt:
            await tg_send_message(chat_id, "Не поняла дату. Пример: 1992-08-14 или 14.08.1992")
            return {"ok": True}
        d["date"] = dt.isoformat()
        sess["state"] = "ASK_TIME"
        await tg_send_message(chat_id, "Отлично. Введи время рождения (HH:MM), например 07:30")
        return {"ok": True}

    if state == "ASK_TIME":
        tm = parse_time(text)
        if not tm:
            await tg_send_message(chat_id, "Не поняла время. Пример: 07:30 (24-часовой формат)")
            return {"ok": True}
        d["time"] = f"{tm[0]:02d}:{tm[1]:02d}"
        sess["state"] = "ASK_CITY"
        await tg_send_message(chat_id, "Город рождения? (например: Barcelona)")
        return {"ok": True}

       if state == "ASK_CITY":
        # принимаем "City", либо "City, Country", либо "City / Country"
        normalized = text.replace("/", ",")
        parts = [p.strip() for p in normalized.split(",") if p.strip()]

        if len(parts) >= 2:
            d["city"] = parts[0]
            d["country"] = parts[1]
            sess["state"] = "ASK_TZ"
            await tg_send_message(chat_id,
                "Ок. Теперь часовой пояс в формате IANA.\n"
                "Пример: Europe/Amsterdam или Europe/Madrid"
            )
        else:
            d["city"] = text.strip()
            sess["state"] = "ASK_COUNTRY"
            await tg_send_message(chat_id, "Страна рождения? (например: Russia)")
        return {"ok": True}

    if state == "ASK_COUNTRY":
        d["country"] = text.strip()
        sess["state"] = "ASK_TZ"
        await tg_send_message(chat_id,
            "Часовой пояс в формате IANA.\n"
            "Пример: Europe/Amsterdam или Europe/Madrid"
        )
        return {"ok": True}

    if state == "ASK_TZ":
        if "/" not in text or " " in text:
            await tg_send_message(chat_id, "Похоже на неверный формат. Пример: Europe/Amsterdam")
            return {"ok": True}
        d["tz"] = text.strip()
        sess["state"] = "ASK_TOPIC"
        await tg_send_message(chat_id, "Теперь выбери тему 👇", reply_markup=TOPIC_KEYBOARD)
        return {"ok": True}


    if state == "ASK_FREEFORM":
        if not OPENAI_API_KEY:
            await tg_send_message(chat_id, "Бот запущен, но не настроен OPENAI_API_KEY.")
            return {"ok": True}

        # Геокодинг
        try:
            coords = await geocode_city(d["city"], d["country"])
        except Exception:
            coords = None
        if not coords:
            await tg_send_message(chat_id,
                "Не смогла найти координаты города 😕\n"
                "Попробуй написать город/страну на английском или крупнее (например: Moscow, Russia)."
            )
            sess["state"] = "ASK_CITY"
            return {"ok": True}

        d["lat"], d["lon"] = coords[0], coords[1]

        # Считаем карту
        y, m, day = map(int, d["date"].split("-"))
        hh, mm = map(int, d["time"].split(":"))
        dt_local = datetime(y, m, day, hh, mm, 0)

        try:
            chart = compute_chart(d["lat"], d["lon"], dt_local, d["tz"])
            chart_text = chart_to_text(chart)
        except Exception as e:
            await tg_send_message(chat_id, f"Ошибка расчёта карты 😕 ({e})\nПопробуй /reset и введи данные заново.")
            return {"ok": True}

        topic = topic_label(d["topic"])

        system_prompt = f"""
Ты — тёплый и понятный астрологический помощник. Без мистики-страшилок, без фатализма.
Отвечай на русском.
Формат ответа:
- 1 абзац: суть по запросу
- 3–6 буллетов: что это значит + сильные стороны/риски
- 2 практичных шага (что сделать сегодня/на неделе)

Данные натальной карты (тропическая):
{chart_text}

Контекст:
- Тема: {topic}
- Вопрос пользователя: {text}
"""

        answer = await call_openai(system_prompt, text)
        await tg_send_message(chat_id, answer)

        # после ответа — предложим следующий вопрос по той же карте
        sess["state"] = "ASK_TOPIC"
        await tg_send_message(chat_id, "Хочешь ещё один разбор? Выбери тему 👇", reply_markup=TOPIC_KEYBOARD)
        return {"ok": True}

    # fallback
    await tg_send_message(chat_id, "Я чуть потерялась 😅 Напиши /start чтобы начать заново.")
    return {"ok": True}
