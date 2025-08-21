# app.py — Alina Bot | GPT-5 + Notlar + Vade Kontrol
import os, json, base64, logging, pytz, datetime, re
import os, json, base64, logging, pytz, datetime, sqlite3
from datetime import datetime as dt, timezone
from pathlib import Path
from datetime import datetime as dt
from dateparser.search import search_dates

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI

# ---------- LOG ----------
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("alina")

# ---------- ENV ----------
BOT_TOKEN       = os.getenv("TELEGRAM_TOKEN", "").strip()
SA_JSON_B64     = os.getenv("GOOGLE_SA_JSON_B64", "").strip()
GSHEET_NOTES_ID = os.getenv("GSHEET_NOTES_ID", "").strip()
GSHEET_VADE_ID  = os.getenv("GSHEET_VADE_ID", "").strip()
CHAT_ID         = int(os.getenv("CHAT_ID", "8396073279"))
TZ_NAME         = os.getenv("TZ", "Europe/Istanbul")
local_tz        = pytz.timezone(TZ_NAME)

DB              = Path(__file__).with_name("data.db")

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-5").strip()
openai_client   = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Regex to detect reminder commands like "hatırlat" or "/hatirlat"
REMIND_RE = re.compile(r"^\s*(?:/)?hat[ıi]rlat\b", re.IGNORECASE)

# ---------- Google Sheets ----------
def _gs_open(sheet_id: str):
    if not SA_JSON_B64 or not sheet_id:
        return None
    try:
        info = json.loads(base64.b64decode(SA_JSON_B64).decode("utf-8"))
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds  = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)
        return gc.open_by_key(sheet_id)
    except Exception as e:
        log.warning(f"Sheets auth error: {e}")
        return None

def gs_append_note(row_date_local: dt, content: str, chat_id: int):
    sh = _gs_open(GSHEET_NOTES_ID)
    if not sh:
        return
    tab = row_date_local.strftime("%Y-%m-%d")
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=1000, cols=6)
        ws.append_row(["Tarih","Saat","ChatID","İçerik"])
    ws.append_row([
        row_date_local.strftime("%d.%m.%Y"),
        row_date_local.strftime("%H:%M:%S"),
        str(chat_id),
        content
    ])
def gs_append(dt_local: dt, entry_type: str, content: str, chat_id: int):
    sh = _gs_open(GSHEET_NOTES_ID)
    if not sh:
        return
    tab = dt_local.strftime("%Y-%m-%d")
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=1000, cols=6)
        ws.append_row(["Tarih","Saat","Tür","ChatID","İçerik"])
    ws.append_row([
        dt_local.strftime("%d.%m.%Y"),
        dt_local.strftime("%H:%M:%S"),
        entry_type,
        str(chat_id),
        content
    ])
# ---------- GPT-5 ----------
def ai_reply(prompt: str) -> str:
    if not openai_client:
        return "AI yapılandırılmadı (OPENAI_API_KEY ekleyin)."
    try:
        resp = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Adın Alina. Kullanıcının yazdığı dilde net, yardımsever ve kısa cevap ver."
                },
                {"role": "user", "content": prompt}
            ],
            max_completion_tokens=1024
        )
        content = (resp.choices[0].message.content or "").strip()
        return content if content else "Üzgünüm, şu an cevap üretemedim."
    except Exception as e:
        return f"Şu anda yanıt veremiyorum. (Hata: {e})"

# ---------- Vade Kontrol ----------
async def vade_kontrol(context: ContextTypes.DEFAULT_TYPE):
    sh = _gs_open(GSHEET_VADE_ID)
    if not sh:
        return
    try:
        ws = sh.sheet1
        rows = ws.get_all_values()
    except Exception as e:
        log.warning(f"Sheets read error: {e}")
        return

    today = dt.now(local_tz).date()
    uyarilar = []

    for i, row in enumerate(rows[1:], start=2):
        try:
            vade_raw = row[3]   # D sütunu (A=0, B=1, C=2, D=3)
            odendi   = row[14] if len(row) > 14 else ""  # O sütunu (A=0 → O=14)
            if not vade_raw:
                continue

            if str(odendi).strip().upper() == "TRUE":
                continue

            vade_tarih = dt.strptime(vade_raw.strip(), "%Y-%m-%d %H:%M:%S").date()
            if vade_tarih == today:
                aciklama = row[0] if len(row) > 0 else f"Satır {i}"
                uyarilar.append(f"{aciklama} → Vade tarihi bugün ({vade_raw}) | Ödenmedi")
        except Exception as e:
            log.warning(f"Satır {i} hata: {e}")
            continue

    if uyarilar:
        msg = "⏰ Bugün vadesi gelen ve ödenmemiş satırlar:\n" + "\n".join(uyarilar)
        await context.bot.send_message(chat_id=CHAT_ID, text=msg)

# ---------- Handlers ----------
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
    try:
        gs_append(ts_local, "Not", text, chat_id)
    except Exception as e:
        log.warning(f"Sheets note error: {e}")
    await update.message.reply_text(f"Not alındı ✅ ({ts_local.strftime('%d.%m.%Y %H:%M')}).")

async def cmd_hatirlat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Schedule a reminder.

    Usage: /hatirlat <başlık> | <zaman>
    """

    chat_id = update.effective_chat.id
    args    = " ".join(context.args).strip()
    if not args:
        return await update.message.reply_text(
            "Kullanım: /hatirlat <metin | zaman>"
        )

    title, when_text = split_title_time(args)
    title = (title or "").strip() or "Hatırlatma"

    when_utc = parse_when(when_text)
    if not when_utc:
        return await update.message.reply_text(
            "Zamanı anlayamadım. Örn: /hatirlat su iç | yarın 10:30."
        )

    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO reminders(chat_id,title,remind_at_utc,sent) VALUES (?,?,?,0)",
        (chat_id, title, when_utc.isoformat()),
    )
    con.commit(); con.close()

    jobq = context.job_queue or context.application.job_queue
    jobq.run_once(reminder_job, when=when_utc, data={"chat_id": chat_id, "title": title})

    local_str = when_utc.astimezone(local_tz).strftime("%d.%m.%Y %H:%M")
    try:
        gs_append(when_utc.astimezone(local_tz), "Hatırlatma (Planlandı)", title, chat_id)
    except Exception as e:
        log.warning(f"Sheets reminder plan error: {e}")

    await update.message.reply_text(
        f"Tamam! {local_str} için hatırlatma kuruldu: “{title}”"
    )


async def cmd_hatirlat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Schedule a reminder.

    Usage: /hatirlat <başlık> | <zaman>
    """

    chat_id = update.effective_chat.id
    args    = " ".join(context.args).strip()
    if not args:
        return await update.message.reply_text(
            "Kullanım: /hatirlat <metin | zaman>"
        )

    title, when_text = split_title_time(args)
    title = (title or "").strip() or "Hatırlatma"

    when_utc = parse_when(when_text)
    if not when_utc:
        return await update.message.reply_text(
            "Zamanı anlayamadım. Örn: /hatirlat su iç | yarın 10:30."
        )

    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO reminders(chat_id,title,remind_at_utc,sent) VALUES (?,?,?,0)",
        (chat_id, title, when_utc.isoformat()),
    )
    con.commit(); con.close()

    jobq = context.job_queue or context.application.job_queue
    jobq.run_once(reminder_job, when=when_utc, data={"chat_id": chat_id, "title": title})

    local_str = when_utc.astimezone(local_tz).strftime("%d.%m.%Y %H:%M")
    try:
        gs_append(when_utc.astimezone(local_tz), "Hatırlatma (Planlandı)", title, chat_id)
    except Exception as e:
        log.warning(f"Sheets reminder plan error: {e}")

    await update.message.reply_text(
        f"Tamam! {local_str} için hatırlatma kuruldu: “{title}”"
    )
def split_title_time(text: str) -> tuple[str, str]:
    """Split a reminder message into title and time parts.

    Supports messages like "hatırlat yarın 15:00 su iç" or
    "hatırlat su iç | yarın 15:00". Returns a tuple of
    ``(title, when_text)`` where ``when_text`` is the part that
    describes the reminder time. If no time is found the entire
    body is returned as ``when_text`` with an empty title.
    """
    body = REMIND_RE.sub("", text, count=1).strip()
    if "|" in body:
        title, when = [p.strip() for p in body.split("|", 1)]
        return title, when

    result_all = search_dates(body, languages=["tr"])
    if result_all:
        when_text = result_all[-1][0]
        title = body.replace(when_text, "", 1).strip()
        return title, when_text

    words = body.split()
    for i in range(len(words), 0, -1):
        prefix = " ".join(words[:i])
        res = search_dates(prefix, languages=["tr"])
        if res and res[0][0] == prefix:
            when_text = prefix
            title = " ".join(words[i:]).strip()
            return title, when_text

    return "", body


def parse_when(text: str) -> dt | None:
    """Parse a natural language time expression to UTC ``datetime``.

    The parsing uses :func:`dateparser.search.search_dates` with
    Turkish language support. A timezone-aware ``datetime`` in UTC
    is returned. ``None`` is returned if parsing fails.
    """
    if not text:
        return None

    res = search_dates(
        text,
        languages=["tr"],
        settings={"TIMEZONE": TZ_NAME, "RETURN_AS_TIMEZONE_AWARE": True},
    )
    if not res:
        return None
    dt_local = res[0][1]
    return dt_local.astimezone(datetime.timezone.utc)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not txt:
        return  # boş mesaj gelirse hiçbir şey gönderme

    chat_id = update.effective_chat.id

    # Hatırlatma
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

    # Normal sohbet → OpenAI (GPT-5)
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
        gs_append(dt.now(local_tz), "Hatırlatma (Gönderildi)", title, chat_id)
    except Exception as e:
        log.warning(f"Sheets reminder send error: {e}")

async def sweeper(context: ContextTypes.DEFAULT_TYPE):
    """Kaçan hatırlatmaları yakala (worker yeniden başlarsa)."""
    now_iso = dt.now(timezone.utc).isoformat()
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, chat_id, title, remind_at_utc FROM reminders WHERE sent=0 AND remind_at_utc<=?",
        (now_iso,)
    ).fetchall()
    for _id, chat_id, title, ts in rows:
        await context.bot.send_message(chat_id=chat_id, text=f"⏰ (Geç) Hatırlatma: {title}")
        con.execute("UPDATE reminders SET sent=1 WHERE id=?", (_id,))
        try:
            gs_append(dt.now(local_tz), "Hatırlatma (Geç yakalandı)", title, chat_id)
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

import unittest
import datetime
from app import REMIND_RE, split_title_time, parse_when


class UtilsTests(unittest.TestCase):
    def test_remind_regex(self):
        self.assertTrue(REMIND_RE.search("hatirlat test"))
        self.assertTrue(REMIND_RE.search("hatırlat test"))
        self.assertTrue(REMIND_RE.search("/hatirlat test"))
        self.assertFalse(REMIND_RE.search("no match"))

    def test_split_title_time_with_pipe(self):
        title, when = split_title_time("hatırlat su iç | yarın 15:00")
        self.assertEqual(title, "su iç")
        self.assertEqual(when, "yarın 15:00")

    def test_split_title_time_freeform(self):
        title, when = split_title_time("hatırlat yarın 15:00 su iç")
        self.assertEqual(title, "su iç")
        self.assertEqual(when, "yarın 15:00")

    def test_parse_when(self):
        dt_utc = parse_when("1 Ocak 2099 12:00")
        self.assertIsNotNone(dt_utc)
        self.assertEqual(dt_utc.tzinfo, datetime.timezone.utc)
        self.assertEqual((dt_utc.year, dt_utc.month, dt_utc.day), (2099, 1, 1))


if __name__ == "__main__":
    unittest.main()

# ---------- MAIN ----------
def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN eksik (Railway Variables'a ekleyin).")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("not",      cmd_not))
    app.add_handler(CommandHandler("hatirlat", cmd_hatirlat))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Vade kontrolü her sabah 09:00
    app.job_queue.run_daily(
        vade_kontrol,
        time=datetime.time(hour=9, minute=0, tzinfo=local_tz)
    )

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
