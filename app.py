# app.py — Alina Bot | Notlar + Vade Kontrol (Ödenme Durumu ile)

import os, json, base64, logging, pytz, datetime
from datetime import datetime as dt

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

import gspread
from google.oauth2.service_account import Credentials

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
CHAT_ID         = int(os.getenv("CHAT_ID", "0"))
TZ_NAME         = os.getenv("TZ", "Europe/Istanbul")
local_tz        = pytz.timezone(TZ_NAME)

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

    for i, row in enumerate(rows[1:], start=2):  # başlık hariç
        try:
            vade_raw = row[3]   # D sütunu (vade tarihi)
            odendi   = row[14] if len(row) > 14 else ""  # O sütunu (A=0 → O=14)
            if not vade_raw:
                continue

            # Eğer ödenmişse atla
            if str(odendi).strip().upper() == "TRUE":
                continue

            # Tarihi parse et
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

# ---------- Telegram Handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Merhaba, ben Alina 🤖\n"
        "• /not <metin> → not ekler (GSHEET_NOTES_ID içine kaydedilir)\n"
        "• Her gün 09:00’da GSHEET_VADE_ID tablosunda D sütununu kontrol ederim.\n"
        "• Eğer O sütununda TRUE ise (ödenmiş), bildirim yapmam."
    )

async def cmd_not(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text    = " ".join(context.args).strip()
    if not text:
        return await update.message.reply_text("Kullanım: /not <metin>")
    ts_local = dt.now(local_tz)
    try:
        gs_append_note(ts_local, text, chat_id)
    except Exception as e:
        log.warning(f"Sheets note error: {e}")
    await update.message.reply_text(f"Not alındı ✅ ({ts_local.strftime('%d.%m.%Y %H:%M')}).")

# ---------- MAIN ----------
def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN eksik (Railway Variables'a ekleyin).")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("not",   cmd_not))

    # Her gün 09:00'da vade kontrolü çalışsın
    app.job_queue.run_daily(
        vade_kontrol,
        time=datetime.time(hour=9, minute=0, tzinfo=local_tz)
    )

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
