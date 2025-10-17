# app.py — Şekeroğlu Tahsilat Planı (Cloud Uyumlu Sürüm)
import streamlit as st
import pandas as pd
import numpy as np
import io, datetime

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

# === Google Drive Excel bağlantısı (kimlik gerekmez) ===
# 👇 Buraya kendi dosya ID’nizi yazabilirsiniz (Drive'da Paylaş → "Bağlantıya sahip herkes görüntüleyebilir")
EXCEL_FILE_ID = "1C8OpNAIRySkWYTI9jBaboV-Rq85UbVD9"
EXCEL_URL = f"https://drive.google.com/uc?export=download&id={EXCEL_FILE_ID}"

@st.cache_data(ttl=600)
def load_data():
    try:
        # Müşteri ve Evrak sayfalarını oku
        xls = pd.ExcelFile(EXCEL_URL, engine="openpyxl")
        df_musteri = pd.read_excel(xls, sheet_name=0)
        df_evrak = pd.read_excel(xls, sheet_name="Evraklar")
        return df_musteri, df_evrak
    except Exception as e:
        st.error(f"Veri yüklenemedi: {e}")
        return pd.DataFrame(), pd.DataFrame()

df_musteri, df_evrak = load_data()

if df_musteri.empty or df_evrak.empty:
    st.stop()

# === Kullanıcı bazlı filtreleme ===
aktif_kullanici = st.session_state.user

if aktif_kullanici.lower() != "admin":
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

# === Arayüz ===
st.title("💰 ŞEKEROĞLU Tahsilat Planı")
st.markdown("Bu sayfada sadece size bağlı müşterilerin vadeli fatura bilgilerini görüntülüyorsunuz.")

if df_tahsilat.empty:
    st.info("Görüntülenecek tahsilat kaydı bulunamadı.")
else:
    # Sayısal işlemler
    for col in ["Tutar", "Ödenen Tutar"]:
        if col in df_tahsilat.columns:
            df_tahsilat[col] = pd.to_numeric(df_tahsilat[col], errors="coerce").fillna(0.0)
    df_tahsilat["Kalan Tutar"] = (df_tahsilat.get("Tutar", 0) - df_tahsilat.get("Ödenen Tutar", 0)).clip(lower=0.0)

    # Tarihler
    if "Vade Tarihi" in df_tahsilat.columns:
        df_tahsilat["Vade Tarihi"] = pd.to_datetime(df_tahsilat["Vade Tarihi"], errors="coerce")
        df_tahsilat["Kalan Gün"] = (df_tahsilat["Vade Tarihi"] - pd.Timestamp.today()).dt.days
        df_tahsilat["Vade Tarihi"] = df_tahsilat["Vade Tarihi"].dt.strftime("%d/%m/%Y").fillna("")

    # Görünüm
    st.dataframe(
        df_tahsilat[
            ["Müşteri Adı", "Fatura No", "Vade Tarihi", "Kalan Gün", "Tutar", "Ödenen Tutar", "Kalan Tutar", "Satış Temsilcisi"]
        ],
        use_container_width=True
    )

    # Toplam metrik
    toplam_bekleyen = df_tahsilat["Kalan Tutar"].sum()
    geciken = df_tahsilat[df_tahsilat["Kalan Gün"] < 0]["Kalan Tutar"].sum()
    vadesi_gelmemis = df_tahsilat[df_tahsilat["Kalan Gün"] >= 0]["Kalan Tutar"].sum()

    c1, c2, c3 = st.columns(3)
    c1.metric("Toplam Bekleyen Tahsilat", f"{toplam_bekleyen:,.2f} USD")
    c2.metric("Geciken Tahsilatlar", f"{geciken:,.2f} USD")
    c3.metric("Vadeleri Gelmemiş", f"{vadesi_gelmemis:,.2f} USD")

st.markdown("---")
st.caption("© 2025 ŞEKEROĞLU GROUP | Streamlit Cloud CRM – Tahsilat Görünümü")
