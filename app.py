import streamlit as st
import pandas as pd
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
import datetime, io, os

st.set_page_config(page_title="ŞEKEROĞLU Tahsilat Planı", layout="wide")

# === Kullanıcı Sistemi ===
USERS = {
    "export1": "Seker12345!",
    "admin": "Seker12345!",
    "Boss": "Seker12345!",
    "Muhammed": "Seker12345!",
    "Hüseyin": "Seker12345!",
}

if "user" not in st.session_state:
    st.session_state.user = None

def login_screen():
    st.title("🔐 ŞEKEROĞLU Tahsilat Planı Girişi")
    username = st.text_input("Kullanıcı Adı")
    password = st.text_input("Şifre", type="password")
    if st.button("Giriş Yap"):
        if username in USERS and password == USERS[username]:
            st.session_state.user = username
            st.rerun()
        else:
            st.error("Kullanıcı adı veya şifre hatalı.")

if not st.session_state.user:
    login_screen()
    st.stop()

st.sidebar.success(f"Hoş geldiniz, **{st.session_state.user}** 👋")
if st.sidebar.button("Çıkış Yap"):
    st.session_state.user = None
    st.rerun()

# === Google Drive bağlantısı ===
EXCEL_FILE_ID = "1C8OpNAIRySkWYTI9jBaboV-Rq85UbVD9"  # Orijinal CRM'deki dosya ID

@st.cache_resource
def get_drive():
    gauth = GoogleAuth()
    gauth.LocalWebserverAuth()
    return GoogleDrive(gauth)

drive = get_drive()

def load_excel_from_drive(file_id: str):
    downloaded = drive.CreateFile({'id': file_id})
    downloaded.GetContentFile("temp_tahsilat.xlsx")
    df_musteri = pd.read_excel("temp_tahsilat.xlsx", sheet_name=0)
    df_evrak = pd.read_excel("temp_tahsilat.xlsx", sheet_name="Evraklar")
    return df_musteri, df_evrak

try:
    df_musteri, df_evrak = load_excel_from_drive(EXCEL_FILE_ID)
except Exception as e:
    st.error(f"Google Drive'dan veri alınamadı: {e}")
    st.stop()

# === Filtreleme ===
aktif_kullanici = st.session_state.user

if aktif_kullanici.lower() != "admin":
    # Kullanıcının müşterilerini bul
    kendi_musterileri = df_musteri.loc[
        df_musteri["Satış Temsilcisi"].astype(str).str.lower() == aktif_kullanici.lower(),
        "Müşteri Adı"
    ].dropna().unique().tolist()

    if not kendi_musterileri:
        st.warning("Henüz size atanmış müşteri bulunamadı.")
        st.stop()

    df_tahsilat = df_evrak[df_evrak["Müşteri Adı"].isin(kendi_musterileri)].copy()
else:
    df_tahsilat = df_evrak.copy()

# === Görünüm ===
st.title("💰 ŞEKEROĞLU Tahsilat Planı")
st.markdown("Bu sayfada sadece size bağlı müşterilerin vadeli fatura bilgilerini görüntülüyorsunuz.")

if df_tahsilat.empty:
    st.info("Görüntülenecek tahsilat kaydı bulunamadı.")
else:
    # Sayısal kolonları dönüştür
    if "Tutar" in df_tahsilat.columns:
        df_tahsilat["Tutar"] = pd.to_numeric(df_tahsilat["Tutar"], errors="coerce").fillna(0.0)
    if "Ödenen Tutar" in df_tahsilat.columns:
        df_tahsilat["Ödenen Tutar"] = pd.to_numeric(df_tahsilat["Ödenen Tutar"], errors="coerce").fillna(0.0)

    # Kalan tutar
    df_tahsilat["Kalan Tutar"] = (df_tahsilat["Tutar"] - df_tahsilat["Ödenen Tutar"]).clip(lower=0.0)

    # Tarih formatı
    if "Vade Tarihi" in df_tahsilat.columns:
        df_tahsilat["Vade Tarihi"] = pd.to_datetime(df_tahsilat["Vade Tarihi"], errors="coerce").dt.strftime("%d/%m/%Y")

    st.dataframe(
        df_tahsilat[[
            "Müşteri Adı",
            "Fatura No",
            "Vade Tarihi",
            "Tutar",
            "Ödenen Tutar",
            "Kalan Tutar",
            "Satış Temsilcisi"
        ]],
        use_container_width=True
    )

    toplam_tahsilat = df_tahsilat["Kalan Tutar"].sum()
    st.metric("Toplam Bekleyen Tahsilat", f"{toplam_tahsilat:,.2f} USD")

# === Alt bilgi ===
st.markdown("---")
st.caption("© 2025 ŞEKEROĞLU GROUP | Streamlit CRM Tahsilat Görünümü")

