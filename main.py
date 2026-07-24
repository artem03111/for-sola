"""
Traffic Dashboard — FastAPI backend (без pandas, лёгкий по памяти)
Источник данных: опубликованная Google Таблица (CSV export link)
"""

import csv
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from io import StringIO

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

# =============================================================================
# CONFIG
# =============================================================================

CSV_URL = os.getenv("CSV_URL", "")
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "60"))

EXCLUDED_MERCHANTS = {
    "sognolab", "cratecracker", "fiftytemps", "asquad",
    "profit bridge ltd", "ideayard", "skinsbo"
}
CARD_PAY_TYPES = {"visa", "mastercard", "maestro"}
OB_PAY_TYPES   = {"open-banking", "banks/germany"}
SHOW_STATUSES  = {"success", "decline", "processing"}
APPLE_PAY_METHOD_CODE = "69"

HEADER_HINTS = ["merchant", "amount", "status", "currency", "operation", "email", "payment"]

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

_cache = {"rows": [], "updated_at": None, "error": None}
_lock = threading.Lock()

# =============================================================================
# HELPERS
# =============================================================================

def strip_apostrophe(value):
    s = (value or "").strip()
    return s[1:].lstrip() if s.startswith("'") else s


def to_float(x):
    s = (x or "").strip().replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0


def parse_date(x):
    s = (x or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            if fmt is None:
                dt = datetime.fromisoformat(s)
            else:
                dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def find_col(fieldnames, keywords):
    for c in fieldnames:
        low = c.lower()
        if any(k in low for k in keywords):
            return c
    return None


def find_header_row(lines):
    for i, line in enumerate(lines[:10]):
        low = line.lower()
        if sum(1 for h in HEADER_HINTS if h in low) >= 2:
            return i
    return 0


def fetch_csv_text() -> str:
    if not CSV_URL:
        raise RuntimeError("CSV_URL не задан (переменная окружения)")
    resp = requests.get(CSV_URL, timeout=30)
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="replace")


def parse_rows(text: str):
    lines = text.splitlines()
    header_idx = find_header_row(lines)
    trimmed = "\n".join(lines[header_idx:])

    def _read(delim):
        reader = csv.DictReader(StringIO(trimmed), delimiter=delim)
        return reader.fieldnames, list(reader)

    fieldnames, raw_rows = _read(",")
    if not fieldnames or len(fieldnames) <= 1:
        fieldnames, raw_rows = _read(";")
    if not fieldnames:
        return []

    merchant_col = find_col(fieldnames, ["merchant"])
    status_col   = find_col(fieldnames, ["operation_status", "status"])
    optype_col   = find_col(fieldnames, ["operation_type", "type"])
    amount_col   = find_col(fieldnames, ["amount"])
    currency_col = find_col(fieldnames, ["currency"])
    paytype_col  = find_col(fieldnames, ["payment_type_id", "payment_method_type", "payment type"])
    method_col   = find_col(fieldnames, ["payment method", "payment_method"])
    date_col     = find_col(fieldnames, ["created_at", "operation_created_at", "created"])

    out = []
    for r in raw_rows:
        if r is None:
            continue
        merchant_raw = (r.get(merchant_col) or "").strip() if merchant_col else ""
        merchant_low = merchant_raw.lower()
        if not merchant_raw or merchant_low in EXCLUDED_MERCHANTS:
            continue

        status = (r.get(status_col) or "").strip().lower() if status_col else ""
        op_type = (r.get(optype_col) or "").strip().lower() if optype_col else ""
        amt = to_float(r.get(amount_col)) if amount_col else 0.0
        cur = (r.get(currency_col) or "").strip().upper() if currency_col else ""
        pay_type = (r.get(paytype_col) or "").strip().lower() if paytype_col else ""
        pay_method = strip_apostrophe(r.get(method_col)).strip() if method_col else ""
        date = parse_date(r.get(date_col)) if date_col else None

        is_apple = pay_method == APPLE_PAY_METHOD_CODE
        is_card = pay_type in CARD_PAY_TYPES
        is_ob = pay_type in OB_PAY_TYPES
        if is_apple:
            channel = "apple_pay"
        elif is_card:
            channel = "card"
        elif is_ob:
            channel = "open_banking"
        else:
            channel = "other"

        out.append({
            "merchant": merchant_low,
            "merchant_display": merchant_raw,
            "status": status,
            "op_type": op_type,
            "amt": amt,
            "cur": cur,
            "channel": channel,
            "date": date,
        })
    return out


def refresh_cache():
    try:
        text = fetch_csv_text()
        rows = parse_rows(text)
        with _lock:
            _cache["rows"] = rows
            _cache["updated_at"] = datetime.utcnow().isoformat() + "Z"
            _cache["error"] = None
    except Exception as e:
        with _lock:
            _cache["error"] = str(e)


def background_refresh_loop():
    while True:
        time.sleep(REFRESH_SECONDS)
        refresh_cache()


@app.on_event("startup")
def on_startup():
    t = threading.Thread(target=refresh_cache, daemon=True)
    t.start()
    t2 = threading.Thread(target=background_refresh_loop, daemon=True)
    t2.start()


# =============================================================================
# AGGREGATION
# =============================================================================

def agg_block(rows):
    total = len(rows)
    succ = sum(1 for r in rows if r["status"] == "success")
    decl = sum(1 for r in rows if r["status"] == "decline")
    proc = sum(1 for r in rows if r["status"] == "processing")
    amount_success = sum(r["amt"] for r in rows if r["status"] == "success")
    conv = round(succ / total * 100, 2) if total else 0.0
    return {
        "total": total, "success": succ, "decline": decl, "processing": proc,
        "amount_success": round(amount_success, 2), "conversion": conv,
    }


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
            "rows": len(_cache["rows"]),
        }


@app.get("/api/refresh")
def force_refresh():
    refresh_cache()
    with _lock:
        if _cache["error"]:
            raise HTTPException(500, _cache["error"])
        return {"ok": True, "updated_at": _cache["updated_at"], "rows": len(_cache["rows"])}


@app.get("/api/debug")
def debug():
    with _lock:
        rows = list(_cache["rows"])
        error = _cache["error"]

    if not rows:
        return {"error": error or "Нет данных", "rows": 0}

    def top_values(key, n=15):
        counts = {}
        for r in rows:
            counts[r[key]] = counts.get(r[key], 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1])[:n])

    dates = [r["date"] for r in rows if r["date"]]
    return {
        "rows": len(rows),
        "date_min": str(min(dates)) if dates else None,
        "date_max": str(max(dates)) if dates else None,
        "op_type_values": top_values("op_type"),
        "status_values": top_values("status"),
        "channel_values": top_values("channel"),
        "merchant_sample": list({r["merchant_display"] for r in rows})[:15],
    }


@app.get("/api/dashboard")
def dashboard(
    date_from: str = Query(None, description="YYYY-MM-DD"),
    date_to: str = Query(None, description="YYYY-MM-DD"),
    op_type: str = Query("deposit", description="deposit | payout"),
):
    with _lock:
        rows = list(_cache["rows"])
        error = _cache["error"]
        updated_at = _cache["updated_at"]

    if not rows:
        return JSONResponse({"error": error or "Нет данных", "updated_at": updated_at})

    start = end = None
    if date_from:
        start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if date_to:
        end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)

    op_map = {"deposit": {"sale", "payment confirmation"}, "payout": {"payout"}}
    allowed_ops = op_map.get(op_type, op_map["deposit"])

    filtered = []
    for r in rows:
        if r["op_type"] not in allowed_ops:
            continue
        if r["status"] not in SHOW_STATUSES:
            continue
        if start and (r["date"] is None or r["date"] < start):
            continue
        if end and (r["date"] is None or r["date"] >= end):
            continue
        filtered.append(r)

    by_channel = {}
    for ch, label in [("card", "Card"), ("apple_pay", "Apple Pay"), ("open_banking", "Open Banking")]:
        sub = [r for r in filtered if r["channel"] == ch]
        if sub:
            by_channel[label] = agg_block(sub)

    brand_groups = {}
    for r in filtered:
        brand_groups.setdefault(r["merchant_display"], []).append(r)

    by_brand = []
    for merchant, sub in brand_groups.items():
        block = agg_block(sub)
        block["brand"] = merchant
        by_brand.append(block)
    by_brand.sort(key=lambda x: x["total"], reverse=True)

    bc_groups = {}
    for r in filtered:
        bc_groups.setdefault((r["merchant_display"], r["channel"]), []).append(r)

    by_brand_channel = []
    for (merchant, ch), sub in bc_groups.items():
        block = agg_block(sub)
        block["brand"] = merchant
        block["channel"] = ch
        by_brand_channel.append(block)

    overall = agg_block(filtered)

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
