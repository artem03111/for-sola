"""
Traffic Dashboard — FastAPI backend
Источник данных: опубликованная Google Таблица (CSV export link)
"""

import os
import re
import threading
import time
from datetime import datetime
from io import StringIO

import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

# =============================================================================
# CONFIG
# =============================================================================

# Ссылка вида:
# https://docs.google.com/spreadsheets/d/e/2PACX-xxxx/pub?gid=0&single=true&output=csv
CSV_URL = os.getenv("CSV_URL", "")

REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "60"))  # как часто фоново обновлять кэш

EXCLUDED_MERCHANTS = {
    "sognolab", "cratecracker", "fiftytemps", "asquad",
    "profit bridge ltd", "ideayard", "skinsbo"
}
CARD_PAY_TYPES = {"visa", "mastercard", "maestro"}
OB_PAY_TYPES   = {"open-banking", "banks/germany"}
SHOW_STATUSES  = {"success", "decline", "processing"}
APPLE_PAY_METHOD_CODE = "69"

# =============================================================================
# APP
# =============================================================================

app = FastAPI(title="Traffic Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# CACHE (фоновое обновление, чтобы дашборд открывался мгновенно)
# =============================================================================

_cache = {"df": pd.DataFrame(), "updated_at": None, "error": None}
_lock = threading.Lock()


def norm_key(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def strip_apostrophe(value):
    s = str(value or "").strip()
    return s[1:].lstrip() if s.startswith("'") else s


def find_col(df, keywords):
    for c in df.columns:
        if any(k in c.lower() for k in keywords):
            return c
    return None


def to_float(x):
    s = str(x or "").strip().replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0


HEADER_HINTS = ["merchant", "amount", "status", "currency", "operation", "email", "payment"]


def _find_header_row(lines):
    for i, line in enumerate(lines[:10]):
        low = line.lower()
        if sum(1 for h in HEADER_HINTS if h in low) >= 2:
            return i
    return 0


def fetch_sheet_df() -> pd.DataFrame:
    if not CSV_URL:
        raise RuntimeError("CSV_URL не задан (переменная окружения)")
    resp = requests.get(CSV_URL, timeout=30)
    resp.raise_for_status()
    text = resp.content.decode("utf-8", errors="replace")
    lines = text.splitlines()

    header_idx = _find_header_row(lines)
    trimmed = "\n".join(lines[header_idx:])

    def _parse(sep):
        return pd.read_csv(
            StringIO(trimmed), sep=sep, dtype=str, keep_default_na=False,
            on_bad_lines="skip",
        )

    df = _parse(",")
    if df.shape[1] <= 1:
        df = _parse(";")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    merchant_col = find_col(df, ["merchant_name", "merchant name", "merchant"])
    status_col   = find_col(df, ["operation_status", "status"])
    optype_col   = find_col(df, ["operation_type", "type"])
    amount_col   = find_col(df, ["amount"])
    currency_col = find_col(df, ["currency"])
    paytype_col  = find_col(df, ["payment_type_id", "payment_method_type", "payment type"])
    method_col   = find_col(df, ["payment method", "payment_method"])
    date_col     = find_col(df, ["created_at", "operation_created_at", "created"])

    work = pd.DataFrame(index=df.index)
    work["merchant"] = df[merchant_col].astype(str).str.strip().str.lower() if merchant_col else "unknown"
    work["merchant_display"] = df[merchant_col].astype(str).str.strip() if merchant_col else "Unknown"
    work["status"] = df[status_col].astype(str).str.strip().str.lower() if status_col else ""
    work["op_type"] = df[optype_col].astype(str).str.strip().str.lower() if optype_col else ""
    work["amt"] = df[amount_col].apply(to_float) if amount_col else 0.0
    work["cur"] = df[currency_col].astype(str).str.strip().str.upper() if currency_col else ""
    work["pay_type"] = df[paytype_col].astype(str).str.strip().str.lower() if paytype_col else ""
    work["pay_method"] = df[method_col].astype(str).map(strip_apostrophe).str.strip() if method_col else ""

    if date_col:
        work["date"] = pd.to_datetime(df[date_col], utc=True, errors="coerce")
    else:
        work["date"] = pd.Series(pd.NaT, index=work.index, dtype="datetime64[ns, UTC]")

    # на всякий случай гарантируем tz-aware dtype (могло съехать при пустой/смешанной колонке)
    if work["date"].dt.tz is None:
        work["date"] = work["date"].dt.tz_localize("UTC")

    work = work[~work["merchant"].isin(EXCLUDED_MERCHANTS)]

    is_apple = work["pay_method"] == APPLE_PAY_METHOD_CODE
    is_card = work["pay_type"].isin(CARD_PAY_TYPES)
    is_ob = work["pay_type"].isin(OB_PAY_TYPES)

    def channel(row_apple, row_card, row_ob):
        if row_apple:
            return "apple_pay"
        if row_card:
            return "card"
        if row_ob:
            return "open_banking"
        return "other"

    work["channel"] = [
        channel(a, c, o) for a, c, o in zip(is_apple, is_card & ~is_apple, is_ob)
    ]

    return work


def refresh_cache():
    try:
        raw = fetch_sheet_df()
        prepared = prepare_df(raw)
        with _lock:
            _cache["df"] = prepared
            _cache["updated_at"] = datetime.utcnow().isoformat() + "Z"
            _cache["error"] = None
    except Exception as e:
        with _lock:
            _cache["error"] = str(e)


def background_refresh_loop():
    while True:
        refresh_cache()
        time.sleep(REFRESH_SECONDS)


@app.on_event("startup")
def on_startup():
    refresh_cache()  # первая загрузка сразу при старте
    t = threading.Thread(target=background_refresh_loop, daemon=True)
    t.start()


# =============================================================================
# API
# =============================================================================

@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/api/status")
def status():
    with _lock:
        return {
            "updated_at": _cache["updated_at"],
            "error": _cache["error"],
            "rows": len(_cache["df"]),
        }


@app.get("/api/refresh")
def force_refresh():
    refresh_cache()
    with _lock:
        if _cache["error"]:
            raise HTTPException(500, _cache["error"])
        return {"ok": True, "updated_at": _cache["updated_at"], "rows": len(_cache["df"])}


@app.get("/api/debug")
def debug():
    with _lock:
        df = _cache["df"].copy()
        error = _cache["error"]

    if df.empty:
        return {"error": error or "Нет данных", "columns": []}

    def top_values(col, n=15):
        return df[col].value_counts(dropna=False).head(n).to_dict()

    return {
        "rows": len(df),
        "date_min": str(df["date"].min()),
        "date_max": str(df["date"].max()),
        "op_type_values": {str(k): v for k, v in top_values("op_type").items()},
        "status_values": {str(k): v for k, v in top_values("status").items()},
        "channel_values": {str(k): v for k, v in top_values("channel").items()},
        "merchant_sample": df["merchant_display"].dropna().unique()[:15].tolist(),
    }


@app.get("/api/dashboard")
def dashboard(
    date_from: str = Query(None, description="YYYY-MM-DD"),
    date_to: str = Query(None, description="YYYY-MM-DD"),
    op_type: str = Query("deposit", description="deposit | payout"),
):
    with _lock:
        df = _cache["df"].copy()
        error = _cache["error"]
        updated_at = _cache["updated_at"]

    if df.empty:
        return JSONResponse({"error": error or "Нет данных", "updated_at": updated_at})

    if date_from:
        start = pd.to_datetime(date_from, utc=True)
        df = df[df["date"] >= start]
    if date_to:
        end = pd.to_datetime(date_to, utc=True) + pd.Timedelta(days=1)
        df = df[df["date"] < end]

    op_map = {"deposit": {"sale", "payment confirmation"}, "payout": {"payout"}}
    df = df[df["op_type"].isin(op_map.get(op_type, op_map["deposit"]))]
    df = df[df["status"].isin(SHOW_STATUSES)]

    def agg_block(sub: pd.DataFrame):
        total = len(sub)
        succ = int((sub["status"] == "success").sum())
        decl = int((sub["status"] == "decline").sum())
        proc = int((sub["status"] == "processing").sum())
        amt_success = float(sub.loc[sub["status"] == "success", "amt"].sum())
        conv = round(succ / total * 100, 2) if total else 0.0
        return {
            "total": total, "success": succ, "decline": decl, "processing": proc,
            "amount_success": round(amt_success, 2), "conversion": conv,
        }

    # по каналу оплаты
    by_channel = {}
    for ch, label in [("card", "Card"), ("apple_pay", "Apple Pay"), ("open_banking", "Open Banking")]:
        sub = df[df["channel"] == ch]
        if len(sub):
            by_channel[label] = agg_block(sub)

    # по бренду/мерчанту
    by_brand = []
    for merchant, sub in df.groupby("merchant_display"):
        if not merchant:
            continue
        block = agg_block(sub)
        block["brand"] = merchant
        by_brand.append(block)
    by_brand.sort(key=lambda x: x["total"], reverse=True)

    # по бренду x каналу (для детальной разбивки)
    by_brand_channel = []
    for (merchant, ch), sub in df.groupby(["merchant_display", "channel"]):
        if not merchant:
            continue
        block = agg_block(sub)
        block["brand"] = merchant
        block["channel"] = ch
        by_brand_channel.append(block)

    overall = agg_block(df)

    return {
        "updated_at": updated_at,
        "overall": overall,
        "by_channel": by_channel,
        "by_brand": by_brand,
        "by_brand_channel": by_brand_channel,
    }


@app.get("/app", response_class=HTMLResponse)
def serve_app():
    path = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>index.html not found</h1>", status_code=404)
