"""KI-Chat-Assistent für Websites (ИИ-агент для сайтов).

Сервер на FastAPI:
  GET  /            — демо-страница с подключённым виджетом
  GET  /widget.js   — виджет чата, который вставляется на сайт клиента одной строкой
  POST /api/chat    — принимает переписку, спрашивает Claude, возвращает ответ
  GET  /health      — проверка, что сервер жив

Настройки — переменные окружения (см. .env.example).
Знания о бизнесе — в файле knowledge.md (или по пути из KNOWLEDGE_FILE).
"""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from typing import List, Literal

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(BASE_DIR / ".env")

MODEL = os.environ.get("MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("EFFORT", "low")  # low | medium | high — для чата хватает low
KNOWLEDGE_FILE = Path(os.environ.get("KNOWLEDGE_FILE", BASE_DIR / "knowledge.md"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_5MIN", "20"))   # сообщений с одного IP за 5 минут
DAILY_LIMIT = int(os.environ.get("DAILY_MESSAGE_LIMIT", "500"))  # защита бюджета: всего сообщений в сутки

# Модели, для которых доступен серверный fallback при отказе (refusal)
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agent")

# ---------- Промпт ----------

RULES = """You are the chat assistant on the website of the business described below.
You talk to website visitors (potential customers).

How to answer:
- Reply in the visitor's language. If unclear, reply in German. In German use the polite "Sie".
- Be warm, clear and short: 1-4 sentences. Plain text only: no Markdown, no asterisks, no headings. For lists use simple lines starting with "–".
- Use ONLY the facts in the knowledge base below. If something is not in it, say honestly that you don't know and offer the phone number or e-mail. Never invent products, prices, opening hours, offers or policies.
- You are an AI assistant. If someone asks whether they are talking to a human, say clearly that you are an AI assistant.
- Stay on topic: questions about this business. Politely steer other topics back.
- Never ask for payment details, passwords or sensitive data (health, religion etc.).

Requests (reservations, appointments, pre-orders, call-backs, enquiries):
- Collect name, phone or e-mail, the desired date/time and the details.
- When you have at least the name and one contact detail, call the tool create_request, then tell the visitor the request was passed on and the team will confirm it. Do not promise a confirmed booking — the team confirms.
- Do not call the tool before you have the name and a contact detail.

KNOWLEDGE BASE:
"""


def system_prompt() -> str:
    knowledge = KNOWLEDGE_FILE.read_text(encoding="utf-8") if KNOWLEDGE_FILE.exists() else "(empty)"
    return RULES + knowledge


TOOLS = [{
    "name": "create_request",
    "description": (
        "Passes a visitor's request (reservation, appointment, pre-order, call-back or enquiry) "
        "to the business owner. Call only when the visitor's name and at least one contact detail "
        "(phone or e-mail) are known. Returns a confirmation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": "Kind of request, e.g. Tischreservierung, Termin, Vorbestellung, Rückruf, Anfrage"},
            "name": {"type": "string"},
            "phone": {"type": "string", "description": "Phone number, if given"},
            "email": {"type": "string", "description": "E-mail, if given"},
            "when": {"type": "string", "description": "Desired date and time, if relevant"},
            "details": {"type": "string", "description": "Number of people, wishes, order contents etc."},
        },
        "required": ["type", "name"],
    },
}]

# ---------- Заявки в Telegram ----------


def notify_telegram(lead: dict) -> bool:
    lines = [f"🔔 Neue Anfrage: {lead.get('type', 'Anfrage')}"]
    for key, label in [("name", "Name"), ("phone", "Telefon"), ("email", "E-Mail"),
                       ("when", "Wann"), ("details", "Details")]:
        if lead.get(key):
            lines.append(f"{label}: {lead[key]}")
    text = "\n".join(lines)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        log.info("Заявка (Telegram не настроен):\n%s", text)
        return False
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data=data, timeout=10
        ) as r:
            return r.status == 200
    except Exception as e:  # заявку не теряем — она в логах
        log.error("Не удалось отправить в Telegram: %s\n%s", e, text)
        return False


def run_tool(name: str, raw_input, leads: list) -> dict:
    if name != "create_request":
        return {"ok": False, "error": f"unknown tool {name}"}
    data = raw_input if isinstance(raw_input, dict) else {}
    lead = {k: str(data.get(k, "")).strip()[:500] for k in ["type", "name", "phone", "email", "when", "details"]}
    if not lead["name"] or not (lead["phone"] or lead["email"]):
        return {"ok": False, "error": "Name and phone or e-mail are required. Ask the visitor for them."}
    notify_telegram(lead)
    leads.append(lead)
    return {"ok": True, "message": "Request passed to the team. They will confirm it."}

# ---------- Защита от спама и перерасхода ----------


_hits: dict = defaultdict(deque)
_day = {"date": "", "count": 0}


def check_limits(ip: str) -> None:
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > 300:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "Zu viele Nachrichten. Bitte versuchen Sie es in ein paar Minuten erneut.")
    today = time.strftime("%Y-%m-%d")
    if _day["date"] != today:
        _day.update(date=today, count=0)
    if _day["count"] >= DAILY_LIMIT:
        raise HTTPException(503, "Der Chat ist heute nicht mehr verfügbar. Bitte rufen Sie uns an.")
    q.append(now)
    _day["count"] += 1

# ---------- API ----------


class Msg(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class ChatIn(BaseModel):
    messages: List[Msg] = Field(min_length=1, max_length=30)


app = FastAPI(title="KI-Chat-Assistent", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["POST", "GET"],
                   allow_headers=["Content-Type"])
_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=2, timeout=60)
    return _client


def ask_claude(history: list) -> tuple:
    """Задаёт вопрос Claude, выполняя инструмент create_request при необходимости."""
    messages = list(history)
    leads: list = []
    extra = {}
    if MODEL in FALLBACK_MODELS:
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    for _ in range(4):
        resp = client().beta.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
            output_config={"effort": EFFORT},
            **extra,
        )
        if resp.stop_reason == "refusal":
            return ("Dazu kann ich leider nichts sagen. Bei Fragen zu unserem Angebot helfe ich gern!", leads)
        if resp.stop_reason == "tool_use":
            # Ответ модели добавляем целиком и без изменений (в т.ч. блоки размышлений)
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = run_tool(block.name, block.input, leads)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(result, ensure_ascii=False),
                                    **({"is_error": True} if not result.get("ok") else {})})
            messages.append({"role": "user", "content": results})
            continue
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return (text or "Entschuldigung, bitte formulieren Sie Ihre Frage noch einmal.", leads)
    return ("Entschuldigung, das hat nicht geklappt. Bitte versuchen Sie es noch einmal.", leads)


@app.post("/api/chat")
def chat(body: ChatIn, request: Request):
    msgs = [m.model_dump() for m in body.messages]
    while msgs and msgs[0]["role"] != "user":  # приветствие виджета не отправляем
        msgs.pop(0)
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "The last message must come from the user.")
    msgs = msgs[-20:]
    if msgs[0]["role"] != "user":
        msgs = msgs[1:]
    ip = (request.headers.get("x-forwarded-for") or request.client.host or "?").split(",")[0].strip()
    check_limits(ip)
    try:
        reply, leads = ask_claude(msgs)
    except anthropic.RateLimitError:
        raise HTTPException(429, "Gerade sind viele Anfragen. Bitte versuchen Sie es gleich noch einmal.")
    except anthropic.APIStatusError as e:
        log.error("Claude API error %s: %s", e.status_code, e.message)
        raise HTTPException(502, "Der Assistent ist gerade nicht erreichbar. Bitte rufen Sie uns an.")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Verbindungsproblem. Bitte versuchen Sie es noch einmal.")
    return {"reply": reply, "leads": [{"type": l["type"], "when": l["when"]} for l in leads]}


@app.get("/widget.js")
def widget():
    return FileResponse(BASE_DIR / "static" / "widget.js", media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=300"})


@app.get("/")
def demo():
    return FileResponse(BASE_DIR / "static" / "demo.html")


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL, "knowledge": KNOWLEDGE_FILE.exists(),
            "telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)}
