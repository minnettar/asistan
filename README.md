# Telegram + Groq AI Bot 🤖

Bu proje, Telegram üzerinden Groq API kullanarak sohbet etmenizi sağlar.
Botun adı **Alina Çelikkalkan**

---

## 🚀 Kullanım

1. Reponun kopyasını alın:
   ```bash
   git clone <repo-link>
   cd <repo-klasoru>
   ```

## Komutlar

- `/not <metin>`: not ekler.
- `/hatirlat <mesaj> | <tarih-saat>`: hatırlatma ayarlar (alias `/hatırlat`).
  Örnek: `/hatirlat su iç | yarın 15:00`.
  
2. Gerekli bağımlılıkları yükleyin:
   ```bash
   pip install -r requirements.txt
   ```

3. Ortam değişkenlerini ayarlayın:
   - `TELEGRAM_TOKEN` – Telegram bot tokenı
   - `OPENAI_API_KEY` – Groq API anahtarı
   - `OPENAI_MODEL` (opsiyonel, varsayılan `gpt-5`)
   - `GOOGLE_SA_JSON_B64` – Google servis hesabı JSON'unun Base64 kodu
   - `GSHEET_NOTES_ID` – Notların kaydedileceği Google Sheet ID'si
   - `GSHEET_VADE_ID` – Vade kontrolü için Google Sheet ID'si
   - `CHAT_ID` – Bildirim gönderilecek Telegram chat ID'si
   - `TZ` (opsiyonel, varsayılan `Europe/Istanbul`)

4. Botu başlatın:
   ```bash
   python app.py
   ```
