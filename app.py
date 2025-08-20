# app.py — Alina (OpenAI-only) | Reminders + Notes + Google Sheets log
# Improved: safer SQLite, robust intent JSON, reliable JobQueue, error resilience

import os, re, json, base64, sqlite3, logging, pytz
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, JobQueue
)

from dateparser.search import search_dates
import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI  # openai>=1.40,<2
from telegram.error import Conflict, NetworkError, RetryAfter

# ---------- LOG ----------
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("alina")

# ---------- ENV ----------
BOT_TOKEN        = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-5").strip()
GSHEET_ID        = os.getenv("GSHEET_ID", "").strip()
SA_JSON_B64      = os.getenv("GOOGLE_SA_JSON_B64", "").strip()
TZ_NAME          = os.getenv("TZ", "Europe/Istanbul")
local_tz         = pytz.timezone(TZ_NAME)

# ---------- AI (OpenAI) ----------
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY eksik; AI yanıtları çalışmayacak.")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def ai_reply(prompt: str) -> str:
    if not openai_client:
        return "AI yapılandırılmadı (OPENAI_API_KEY ekleyin)."
    sys_msg = "Adın Alina Çelikkalkan. Türkçe, net ve yardımsever cevap ver."
    try:
        resp = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user",   "content": prompt}
            ],
            max_completion_tokens=1024
        )
        content = (resp.choices[0].message.content or "").strip()
        return content[:4096] if content else "Üzgünüm, şu an cevap üretemedim."
    except Exception as e:
        return f"Şu anda yanıt veremiyorum. (Hata: {e})"

def _extract_json(raw: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None

def ai_intent(text: str) -> Optional[dict]:
    if not openai_client:
        return None
    sys_msg = (
        "Türkçe niyet sınıflandırıcı ve bilgi çıkarıcı gibi davran.\n"
        "Çıktı sadece GEÇERLİ JSON olsun.\n"
        "Alanlar: intent(note/reminder/chat), title, when_text."
    )
    try:
        resp = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user",   "content": f"Mesaj: {text}"}
            ],
            max_completion_tokens=300
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = _extract_json(raw)
        if not data:
            return None
        intent = (data.get("intent") or "").strip().lower()
        if intent not in {"note", "reminder", "chat"}:
            return None
        data["title"]     = (data.get("title") or "").strip()
        data["when_text"] = (data.get("when_text") or "").strip()
        return data
    except Exception as e:
        log.warning(f"ai_intent error: {e}")
        return None

# ---------- DB ----------
DB = "data.db"
def db_connect():
    return sqlite3.connect(DB, check_same_thread=False, isolation_level=None)

def db_init():
    con = db_connect()
    con.execute("""CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        remind_at_utc TEXT NOT NULL,
        sent INTEGER DEFAULT 0
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_utc TEXT NOT NULL
    )""")
    con.close()

# ---------- Google Sheets ----------
def _gs_client():
    if not SA_JSON_B64 or not GSHEET_ID:
        return None
    try:
        info = json.loads(base64.b64decode(SA_JSON_B64).decode("utf-8"))
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds  = Credentials.from_service_account_info(info, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        log.warning(f"Sheets auth error: {e}")
        return None

def gs_append(row_date_local: datetime, row_type: str, content: str, chat_id: int):
    gc = _gs_client()
    if not gc:
        return
    sh = gc.open_by_key(GSHEET_ID)
    tab = row_date_local.strftime("%Y-%m-%d")
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=1000, cols=6)
        ws.append_row(["Tarih","Saat","ChatID","Tür","İçerik/Durum"])
    ws.append_row([
        row_date_local.strftime("%d.%m.%Y"),
        row_date_local.strftime("%H:%M:%S"),
        str(chat_id),
        row_type,
        content
    ])

# ---------- Zaman/Metin ----------
REMIND_RE = re.compile(r"\b(hat[ıi]rlat|alarm kur|remind)\b", re.IGNORECASE)
NOTE_RE   = re.compile(r"\b(not ?al|yaz|kaydet)\b", re.IGNORECASE)

def normalize_time_text(s: str) -> str:
    s = s.lower()
    s = re.sub(r'(\d{1,2})\.(\d{2})', r'\1:\2', s)
    return re.sub(r'\b(saat|ta|te|da|de)\b', ' ', s)

def parse_when(text: str) -> Optional[datetime]:
    raw = normalize_time_text(text)
    found = search_dates(raw, languages=['tr','en'],
        settings={'TIMEZONE': TZ_NAME,'RETURN_AS_TIMEZONE_AWARE': True,'PREFER_DATES_FROM': 'future'})
    if found:
        return found[-1][1].astimezone(timezone.utc)
    m = re.search(r'(\d{1,2}):(\d{2})', raw)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        now_local = datetime.now(local_tz)
        dt_local  = now_local.replace(hour=h, minute=mi, second=0, microsecond=0)
        if dt_local <= now_local:
            dt_local += timedelta(days=1)
        return dt_local.astimezone(timezone.utc)
    return None

def split_title_time(text: str) -> Tuple[str,str]:
    if "|" in text:
        left, right = text.split("|", 1)
        return REMIND_RE.sub("", left).strip(), right.strip()
    return REMIND_RE.sub("", text).strip(), text

# ---------- Handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Merhaba, ben Alina 🤖\n"
        "• not al Toplantı özetini yaz\n"
        "• hatırlat ilaç al | bugün 21:30\n"
        f"Model: {OPENAI_MODEL}"
    )

async def cmd_not(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text    = " ".join(context.args).strip()
    if not text:
        return await update.message.reply_text("Kullanım: /not <metin>")
    ts_utc = datetime.now(timezone.utc)
    con = db_connect()
    con.execute("INSERT INTO notes(chat_id,text,created_utc) VALUES (?,?,?)",
                (chat_id, text, ts_utc.isoformat()))
    con.close()
    try: gs_append(ts_utc.astimezone(local_tz), "Not", text, chat_id)
    except Exception as e: log.warning(f"Sheets note error: {e}")
    await update.message.reply_text(f"Not alındı ✅ ({ts_utc.astimezone(local_tz):%d.%m.%Y %H:%M}).")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not txt: return
    chat_id = update.effective_chat.id

    # --- Note quick intent ---
    if NOTE_RE.search(txt):
        note_text = NOTE_RE.sub("", txt).strip() or txt
        ts_utc = datetime.now(timezone.utc)
        con = db_connect()
        con.execute("INSERT INTO notes(chat_id,text,created_utc) VALUES (?,?,?)",
                    (chat_id, note_text, ts_utc.isoformat()))
        con.close()
        try: gs_append(ts_utc.astimezone(local_tz), "Not", note_text, chat_id)
        except Exception as e: log.warning(f"Sheets note error: {e}")
        return await update.message.reply_text(f"Not alındı ✅ ({ts_utc.astimezone(local_tz):%d.%m.%Y %H:%M}).")

    # --- Reminder quick intent ---
    if REMIND_RE.search(txt):
        title, when_text = split_title_time(txt)
        when_utc = parse_when(when_text)
        if not when_utc:
            return await update.message.reply_text("Zamanı anlayamadım. Örn: “hatırlat su iç | yarın 10:30”.")
        con = db_connect()
        cur = con.execute("INSERT INTO reminders(chat_id,title,remind_at_utc,sent) VALUES (?,?,?,0)",
                          (chat_id, title or "Hatırlatma", when_utc.isoformat()))
        rid = cur.lastrowid; con.close()
        context.job_queue.run_once(reminder_job, when=when_utc,
                                   data={"chat_id": chat_id, "title": title, "id": rid},
                                   name=f"reminder-{rid}")
        try: gs_append(when_utc.astimezone(local_tz), "Hatırlatma (Planlandı)", title, chat_id)
        except Exception as e: log.warning(f"Sheets reminder plan error: {e}")
        return await update.message.reply_text(f"Tamam! {when_utc.astimezone(local_tz):%d.%m.%Y %H:%M} için hatırlatma kuruldu: “{title}”")

    # --- AI fallback ---
    parsed = ai_intent(txt)
    if parsed and parsed["intent"] in {"note","reminder"}:
        if parsed["intent"]=="note":
            return await handle_text(Update.de_json({"message":{"text":"not al "+parsed["title"]}}, update.bot), context)
        else:
            return await handle_text(Update.de_json({"message":{"text":"hatırlat "+parsed["title"]+" | "+parsed["when_text"]}}, update.bot), context)

    # --- Chat fallback ---
    reply = ai_reply(txt)
    await update.message.reply_text(reply)

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    chat_id, title, rid = int(d.get("chat_id")), (d.get("title") or "Hatırlatma"), int(d.get("id"))
    await context.bot.send_message(chat_id=chat_id, text=f"⏰ Hatırlatma: {title}")
    con = db_connect()
    con.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
    con.close()
    try: gs_append(datetime.now(local_tz), "Hatırlatma (Gönderildi)", title, chat_id)
    except Exception as e: log.warning(f"Sheets reminder send error: {e}")

async def sweeper(context: ContextTypes.DEFAULT_TYPE):
    now_iso = datetime.now(timezone.utc).isoformat()
    con = db_connect()
    rows = con.execute("SELECT id, chat_id, title FROM reminders WHERE sent=0 AND remind_at_utc<=?", (now_iso,)).fetchall()
    for rid, chat_id, title in rows:
        await context.bot.send_message(chat_id=chat_id, text=f"⏰ (Geç) Hatırlatma: {title}")
        con.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
        try: gs_append(datetime.now(local_tz), "Hatırlatma (Geç yakalandı)", title, chat_id)
        except Exception as e: log.warning(f"Sheets late log error: {e}")
    con.close()

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        log.warning("Another polling instance detected; ignoring Conflict."); return
    if isinstance(context.error, RetryAfter):
        log.warning(f"Rate limited; retrying after {context.error.retry_after}s"); return
    if isinstance(context.error, NetworkError):
        log.warning("Network error, continuing..."); return
    log.exception(context.error)

# ---------- MAIN ----------
def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN eksik.")
    db_init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("not",   cmd_not))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(sweeper, interval=20, first=10)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
