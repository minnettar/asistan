# app.py — Alina (OpenAI-only) | Reminders + Notes + Google Sheets log | Model env'den
import os, re, json, base64, sqlite3, logging, pytz
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from dateparser.search import search_dates
import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI  # openai>=1.40,<2

# ---------- LOG ----------
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("alina")

# ---------- ENV ----------
BOT_TOKEN        = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o").strip()  # Örn: gpt-5, gpt-4.1, gpt-4o
GSHEET_ID        = os.getenv("GSHEET_ID", "").strip()            # Spreadsheet ID
SA_JSON_B64      = os.getenv("GOOGLE_SA_JSON_B64", "").strip()   # service-account.json (base64)
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
    resp = openai_client.chat.completions.create(
        model=OPENAI_MODEL,   # Model env'den gelir (gpt-5, gpt-4.1, gpt-4o, vb.)
        messages=[{"role":"system","content":sys_msg},
                  {"role":"user","content":prompt}],
        temperature=0.6,
        max_tokens=1024
    )
    return resp.choices[0].message.content.strip()

# ---------- DB ----------
DB = "data.db"
def db_init():
    con = sqlite3.connect(DB)
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
    con.commit(); con.close()

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
    if not gc: return
    sh = gc.open_by_key(GSHEET_ID)
    tab = row_date_local.strftime("%Y-%m-%d")  # her güne ayrı sheet
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
REMIND_RE = re.compile(r"\b(hatırlat|hatirlat|remind)\b", re.IGNORECASE)

def normalize_time_text(s: str) -> str:
    s = s.lower()
    s = re.sub(r'(\d{1,2})\.(\d{2})', r'\1:\2', s)   # 21.15 -> 21:15
    s = re.sub(r'\b(saat)\b', ' ', s)
    s = re.sub(r'\b(ta|te|da|de)\b', ' ', s)
    return s

def parse_when(text: str) -> Optional[datetime]:
    raw = normalize_time_text(text)
    found = search_dates(
        raw, languages=['tr','en'],
        settings={
            'TIMEZONE': TZ_NAME,
            'RETURN_AS_TIMEZONE_AWARE': True,
            'PREFER_DATES_FROM': 'future'
        }
    )
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

def split_title_time(text: str):
    if "|" in text:
        left, right = text.split("|", 1)
        title = REMIND_RE.sub("", left).strip() or left.strip()
        when_text = right.strip()
    else:
        title = REMIND_RE.sub("", text).strip() or text.strip()
        when_text = text
    return title, when_text

# ---------- Telegram ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Merhaba, ben Alina 🤖\n"
        "Örnekler:\n"
        "• hatırlat yarın 15:00 su iç\n"
        "• hatırlat ilaç al | bugün 21:30\n"
        "• /not Toplantı özetini hazırla\n"
        f"Model: {OPENAI_MODEL}"
    )

async def cmd_not(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text    = " ".join(context.args).strip()
    if not text:
        return await update.message.reply_text("Kullanım: /not <metin>")
    ts_utc = datetime.now(timezone.utc)
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO notes(chat_id,text,created_utc) VALUES (?,?,?)",
                (chat_id, text, ts_utc.isoformat()))
    con.commit(); con.close()
    ts_local = ts_utc.astimezone(local_tz)
    try: gs_append(ts_local, "Not", text, chat_id)
    except Exception as e: log.warning(f"Sheets note error: {e}")
    await update.message.reply_text(f"Not alındı ✅ ({ts_local.strftime('%d.%m.%Y %H:%M')}).")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt     = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # Hatırlatma
    if REMIND_RE.search(txt):
        title, when_text = split_title_time(txt)
        when_utc = parse_when(when_text)
        if not when_utc:
            return await update.message.reply_text("Zamanı anlayamadım. Örn: “hatırlat su iç | yarın 10:30”.")
        # DB
        con = sqlite3.connect(DB)
        con.execute("INSERT INTO reminders(chat_id,title,remind_at_utc,sent) VALUES (?,?,?,0)",
                    (chat_id, title, when_utc.isoformat()))
        con.commit(); con.close()
        # JobQueue
        context.job_queue.run_once(reminder_job, when=when_utc, data={"chat_id":chat_id, "title":title})
        local_str = when_utc.astimezone(local_tz).strftime("%d.%m.%Y %H:%M")
        try: gs_append(when_utc.astimezone(local_tz), "Hatırlatma (Planlandı)", title, chat_id)
        except Exception as e: log.warning(f"Sheets reminder plan error: {e}")
        return await update.message.reply_text(f"Tamam! {local_str} için hatırlatma kuruldu: “{title}”")

    # Normal sohbet → OpenAI
    try:
        await update.message.chat.send_action("typing")
        reply = ai_reply(txt)
    except Exception as e:
        reply = f"Şu anda yanıt veremiyorum. (Hata: {e})"
    await update.message.reply_text(reply)

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    chat_id = int(d.get("chat_id"))
    title   = d.get("title", "Hatırlatma")
    await context.bot.send_message(chat_id=chat_id, text=f"⏰ Hatırlatma: {title}")
    # DB işaretle + Sheets log
    con = sqlite3.connect(DB)
    con.execute(
        "UPDATE reminders SET sent=1 WHERE rowid = (SELECT rowid FROM reminders WHERE chat_id=? AND title=? ORDER BY rowid DESC LIMIT 1)",
        (chat_id, title)
    )
    con.commit(); con.close()
    try: gs_append(datetime.now(local_tz), "Hatırlatma (Gönderildi)", title, chat_id)
    except Exception as e: log.warning(f"Sheets reminder send error: {e}")

async def sweeper(context: ContextTypes.DEFAULT_TYPE):
    """Kaçan hatırlatmaları yakala (worker yeniden başlarsa)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, chat_id, title, remind_at_utc FROM reminders WHERE sent=0 AND remind_at_utc<=?",
        (now_iso,)
    ).fetchall()
    for _id, chat_id, title, ts in rows:
        await context.bot.send_message(chat_id=chat_id, text=f"⏰ (Geç) Hatırlatma: {title}")
        con.execute("UPDATE reminders SET sent=1 WHERE id=?", (_id,))
        try: gs_append(datetime.now(local_tz), "Hatırlatma (Geç yakalandı)", title, chat_id)
        except Exception as e: log.warning(f"Sheets late log error: {e}")
    con.commit(); con.close()

# ---------- MAIN ----------
def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN eksik (Railway Variables'a ekleyin).")
    db_init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("not",   cmd_not))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.job_queue.run_repeating(sweeper, interval=20, first=10)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
