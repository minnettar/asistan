# app.py — Alina (OpenAI-only) | Reminders + Notes + Google Sheets log
# GPT-5 uyumlu (temperature yok, max_completion_tokens var) | Safe JobQueue
# Esnek niyet algılama: yerel regex + GPT-5 JSON çıkarım fallback

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

# ---------- LOG ----------
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("alina")

# ---------- ENV ----------
BOT_TOKEN        = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-5").strip()      # varsayılan gpt-5
GSHEET_ID        = os.getenv("GSHEET_ID", "").strip()              # Spreadsheet ID
SA_JSON_B64      = os.getenv("GOOGLE_SA_JSON_B64", "").strip()     # service-account.json (base64)
TZ_NAME          = os.getenv("TZ", "Europe/Istanbul")
local_tz         = pytz.timezone(TZ_NAME)

# ---------- AI (OpenAI) ----------
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY eksik; AI yanıtları çalışmayacak.")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def ai_reply(prompt: str) -> str:
    """GPT-5: temperature yok, max_completion_tokens var. Boş cevap asla dönmesin."""
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
        if not content:
            return "Üzgünüm, şu an cevap üretemedim."
        return content[:4096]
    except Exception as e:
        return f"Şu anda yanıt veremiyorum. (Hata: {e})"

def ai_intent(text: str) -> Optional[dict]:
    """
    Kararsız durumda GPT-5'ten yapılandırılmış niyet çıkarımı ister.
    Beklenen JSON şeması:
      {
        "intent": "note" | "reminder" | "chat",
        "title": "<metin veya boş>",
        "when_text": "<doğal zaman ifadesi veya boş>"
      }
    """
    if not openai_client:
        return None
    sys_msg = (
        "Türkçe niyet sınıflandırıcı ve bilgi çıkarıcı gibi davran.\n"
        "Girdi: kullanıcı mesajı.\n"
        "Çıktı: Sadece GEÇERLİ JSON döndür (ekstra metin YOK).\n"
        "Alanlar:\n"
        "- intent: 'note' (not alma), 'reminder' (hatırlatma) veya 'chat' (sohbet)\n"
        "- title: not ya da hatırlatma başlığı (yoksa boş string)\n"
        "- when_text: hatırlatma zamanı doğal dil (yoksa boş string)\n"
        "Kurallar:\n"
        "- 'not al', 'not alır mısın', 'yazar mısın' vb. -> note\n"
        "- 'hatırlat', 'hatırlatır mısın', 'alarm kur', 'bana ... hatırlat' vb. -> reminder\n"
        "- Zaman ifadesi barizse when_text'e yaz (örn: 'yarın 10:30', '2 dakika sonra').\n"
        "- JSON dışında hiçbir şey yazma."
    )
    user_msg = f"Mesaj: {text}"
    try:
        resp = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user",   "content": user_msg}
            ],
            max_completion_tokens=300
        )
        raw = (resp.choices[0].message.content or "").strip()
        # JSON güvenliği
        start = raw.find("{")
        end   = raw.rfind("}")
        if start == -1 or end == -1:
            return None
        data = json.loads(raw[start:end+1])
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
    if not gc:
        return
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
REMIND_RE = re.compile(
    r"\b(hat[ıi]rlat(?:[ıi]r m[ıi]s[ıi]n)?|alarm kur|bana .* hat[ıi]rlat|remind)\b",
    re.IGNORECASE
)
NOTE_RE = re.compile(
    r"\b(not ?al(?:[ıi]r m[ıi]s[ıi]n)?|yaz(ar m[ıi]s[ıi]n)?|kaydet)\b",
    re.IGNORECASE
)

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

def split_title_time(text: str) -> Tuple[str, str]:
    if "|" in text:
        left, right = text.split("|", 1)
        title = REMIND_RE.sub("", left).strip() or left.strip()
        when_text = right.strip()
    else:
        title = REMIND_RE.sub("", text).strip() or text.strip()
        when_text = text
    return title, when_text

# ---------- Handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Merhaba, ben Alina 🤖\n"
        "Örnekler:\n"
        "• not al Toplantı özetini yaz\n"
        "• hatırlat ilaç al | bugün 21:30\n"
        "• yarın 10:30 su iç  (anahtar kelime olmasa da denerim)\n"
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
    try:
        gs_append(ts_local, "Not", text, chat_id)
    except Exception as e:
        log.warning(f"Sheets note error: {e}")
    await update.message.reply_text(f"Not alındı ✅ ({ts_local.strftime('%d.%m.%Y %H:%M')}).")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not txt:
        return

    chat_id = update.effective_chat.id

    # 1) Yerel hızlı niyet: NOT
    if NOTE_RE.search(txt):
        note_text = NOTE_RE.sub("", txt).strip() or txt
        ts_utc = datetime.now(timezone.utc)
        con = sqlite3.connect(DB)
        con.execute("INSERT INTO notes(chat_id,text,created_utc) VALUES (?,?,?)",
                    (chat_id, note_text, ts_utc.isoformat()))
        con.commit(); con.close()
        ts_local = ts_utc.astimezone(local_tz)
        try:
            gs_append(ts_local, "Not", note_text, chat_id)
        except Exception as e:
            log.warning(f"Sheets note error: {e}")
        return await update.message.reply_text(f"Not alındı ✅ ({ts_local.strftime('%d.%m.%Y %H:%M')}).")

    # 2) Yerel hızlı niyet: HATIRLATMA
    if REMIND_RE.search(txt):
        title, when_text = split_title_time(txt)
        title = (title or "").strip() or "Hatırlatma"
        when_utc = parse_when(when_text)
        if not when_utc:
            return await update.message.reply_text("Zamanı anlayamadım. Örn: “hatırlat su iç | yarın 10:30”.")
        con = sqlite3.connect(DB)
        con.execute("INSERT INTO reminders(chat_id,title,remind_at_utc,sent) VALUES (?,?,?,0)",
                    (chat_id, title, when_utc.isoformat()))
        con.commit(); con.close()
        jobq = context.job_queue or context.application.job_queue
        jobq.run_once(reminder_job, when=when_utc, data={"chat_id": chat_id, "title": title})
        local_str = when_utc.astimezone(local_tz).strftime("%d.%m.%Y %H:%M")
        try:
            gs_append(when_utc.astimezone(local_tz), "Hatırlatma (Planlandı)", title, chat_id)
        except Exception as e:
            log.warning(f"Sheets reminder plan error: {e}")
        return await update.message.reply_text(f"Tamam! {local_str} için hatırlatma kuruldu: “{title}”")

    # 3) AI fallback niyet çıkarımı (esneklik)
    parsed = ai_intent(txt)
    if parsed:
        intent = parsed["intent"]
        if intent == "note":
            note_text = parsed["title"] or txt
            ts_utc = datetime.now(timezone.utc)
            con = sqlite3.connect(DB)
            con.execute("INSERT INTO notes(chat_id,text,created_utc) VALUES (?,?,?)",
                        (chat_id, note_text, ts_utc.isoformat()))
            con.commit(); con.close()
            ts_local = ts_utc.astimezone(local_tz)
            try:
                gs_append(ts_local, "Not", note_text, chat_id)
            except Exception as e:
                log.warning(f"Sheets note error: {e}")
            return await update.message.reply_text(f"Not alındı ✅ ({ts_local.strftime('%d.%m.%Y %H:%M')}).")

        if intent == "reminder":
            title = (parsed["title"] or "Hatırlatma").strip()
            when_text = parsed["when_text"] or txt
            when_utc  = parse_when(when_text)
            if not when_utc:
                return await update.message.reply_text("Zamanı anlayamadım. Örn: “hatırlat su iç | yarın 10:30”.")
            con = sqlite3.connect(DB)
            con.execute("INSERT INTO reminders(chat_id,title,remind_at_utc,sent) VALUES (?,?,?,0)",
                        (chat_id, title, when_utc.isoformat()))
            con.commit(); con.close()
            jobq = context.job_queue or context.application.job_queue
            jobq.run_once(reminder_job, when=when_utc, data={"chat_id": chat_id, "title": title})
            local_str = when_utc.astimezone(local_tz).strftime("%d.%m.%Y %H:%M")
            try:
                gs_append(when_utc.astimezone(local_tz), "Hatırlatma (Planlandı)", title, chat_id)
            except Exception as e:
                log.warning(f"Sheets reminder plan error: {e}")
            return await update.message.reply_text(f"Tamam! {local_str} için hatırlatma kuruldu: “{title}”")

    # 4) Aksi halde normal sohbet → GPT-5
    reply = ai_reply(txt)
    await update.message.reply_text(reply or "Üzgünüm, şu an cevap üretemedim.")

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data or {}
    chat_id = int(d.get("chat_id"))
    title   = (d.get("title") or "").strip() or "Hatırlatma"

    await context.bot.send_message(chat_id=chat_id, text=f"⏰ Hatırlatma: {title}")

    con = sqlite3.connect(DB)
    con.execute(
        "UPDATE reminders SET sent=1 WHERE rowid = (SELECT rowid FROM reminders WHERE chat_id=? AND title=? ORDER BY rowid DESC LIMIT 1)",
        (chat_id, title)
    )
    con.commit(); con.close()
    try:
        gs_append(datetime.now(local_tz), "Hatırlatma (Gönderildi)", title, chat_id)
    except Exception as e:
        log.warning(f"Sheets reminder send error: {e}")

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
        try:
            gs_append(datetime.now(local_tz), "Hatırlatma (Geç yakalandı)", title, chat_id)
        except Exception as e:
            log.warning(f"Sheets late log error: {e}")
    con.commit(); con.close()

# (opsiyonel) basit error handler
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import Conflict
    if isinstance(context.error, Conflict):
        log.warning("Another polling instance detected; ignoring Conflict.")
        return
    log.exception(context.error)

# ---------- MAIN ----------
def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN eksik (Railway Variables'a ekleyin).")
    db_init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Güvenli JobQueue başlatma (PTB 21.x)
    jq = app.job_queue
    if jq is None:
        jq = JobQueue()
        jq.set_application(app)
        jq.start()
        app.job_queue = jq

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("not",   cmd_not))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    # periyodik süpürücü (kaçan hatırlatmalar)
    app.job_queue.run_repeating(sweeper, interval=20, first=10)

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
