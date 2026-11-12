"""FinTech Bank Statement Analyzer API.

The application intentionally keeps only the latest analysed statement in RAM.
No customer statement or transaction is written to a persistent database.
"""
from __future__ import annotations

import os
import re
import json
import traceback
import secrets
import time
from threading import RLock
from typing import Optional

import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from analyzer_engine import (
    detect_bank_format, diagnose_pdf,
    parse_federal_pdf, parse_hdfc_pdf, parse_axis_pdf, parse_idfc_pdf,
    parse_sbi_pdf, parse_icici_pdf, parse_standard_chartered_pdf,
    parse_universal_pdf, compute_underwriting_insights, tag_recurring_salary,
)
from export_service import create_excel_report, create_los_json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

# Browser-session-isolated RAM storage. Nothing is written to a persistent DB.
# Each browser gets an opaque HttpOnly cookie; one visitor cannot read another
# visitor's latest statement by calling the dashboard/export endpoints.
_STATE_LOCK = RLock()
_SESSIONS = {}
_SESSION_TTL_SECONDS = 4 * 60 * 60
_MAX_SESSIONS = 500
_EMPTY_STATE = {"metadata": None, "records": [], "insights": None, "filename": None}


def _session_id(request: Request, response: Response) -> str:
    now = time.time()
    sid = request.cookies.get("statement_session")
    with _STATE_LOCK:
        # Expire inactive browser sessions and cap memory use.
        expired = [key for key, value in _SESSIONS.items() if now - value["last_seen"] > _SESSION_TTL_SECONDS]
        for key in expired:
            _SESSIONS.pop(key, None)
        if not sid or sid not in _SESSIONS:
            sid = secrets.token_urlsafe(32)
            _SESSIONS[sid] = {"state": dict(_EMPTY_STATE), "last_seen": now}
            response.set_cookie(
                key="statement_session", value=sid, max_age=_SESSION_TTL_SECONDS,
                httponly=True, secure=(request.url.scheme == "https"),
                samesite="lax", path="/",
            )
        else:
            _SESSIONS[sid]["last_seen"] = now
        if len(_SESSIONS) > _MAX_SESSIONS:
            oldest = sorted(_SESSIONS, key=lambda key: _SESSIONS[key]["last_seen"])
            for key in oldest[:len(_SESSIONS) - _MAX_SESSIONS]:
                _SESSIONS.pop(key, None)
        return sid


def _store_statement(sid: str, metadata, records, insights, filename):
    with _STATE_LOCK:
        if sid not in _SESSIONS:
            _SESSIONS[sid] = {"state": dict(_EMPTY_STATE), "last_seen": time.time()}
        _SESSIONS[sid]["state"] = {
            "metadata": metadata, "records": records,
            "insights": insights, "filename": filename,
        }
        _SESSIONS[sid]["last_seen"] = time.time()


def _clear_statement(sid: str):
    with _STATE_LOCK:
        if sid in _SESSIONS:
            _SESSIONS[sid]["state"] = dict(_EMPTY_STATE)
            _SESSIONS[sid]["last_seen"] = time.time()

app = FastAPI(title="FinTech Bank Statement Analyzer & Underwriting Suite", version="4.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")



def _parse_statement(content: bytes, password: Optional[str], filename: str):
    detection = detect_bank_format(file_bytes=content, password=password)
    if detection.get("is_encrypted"):
        if detection.get("auth_failed"):
            raise ValueError("Incorrect PDF password. Please try again.")
        raise PermissionError("This PDF is password protected. Enter its password to continue.")

    bank = detection.get("detected_bank", "Generic")
    if "Federal" in bank:
        records, metadata = parse_federal_pdf(content, password=password)
    elif "SBI" in bank or "STATE BANK" in bank:
        records, metadata = parse_sbi_pdf(content, password=password)
    elif "Axis" in bank:
        records, metadata = parse_axis_pdf(content, password=password)
    elif "HDFC" in bank:
        records, metadata = parse_hdfc_pdf(content, password=password)
    elif "IDFC" in bank:
        records, metadata = parse_idfc_pdf(content, password=password)
    elif "ICICI" in bank:
        records, metadata = parse_icici_pdf(content, password=password)
    elif "Standard Chartered" in bank:
        records, metadata = parse_standard_chartered_pdf(content, password=password)
    else:
        records, metadata = parse_universal_pdf(content, password=password, detected_bank=bank)

    if not records:
        fallback_records, fallback_meta = parse_universal_pdf(content, password=password, detected_bank=bank)
        if len(fallback_records) > len(records):
            records, metadata = fallback_records, fallback_meta

    if not records:
        diag = diagnose_pdf(content, password=password)
        reason = "No transactions could be extracted."
        if diag.get("looks_scanned"):
            reason += " This appears to be a scanned PDF; OCR is not enabled in this build."
        raise ValueError(f"{reason} Bank detected: {bank}; pages: {diag.get('pages', 'unknown')}.")

    metadata = dict(metadata or {})
    metadata["bank"] = metadata.get("bank") or bank
    records = tag_recurring_salary(records)
    for row in records:
        row["bank"] = metadata["bank"]
        row["account_no"] = metadata.get("account_no", "")
    insights = compute_underwriting_insights(records, metadata)
    return detection, records, metadata, insights


def _snapshot(sid: str):
    with _STATE_LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return None
        state = session["state"]
        if not state["metadata"] or not state["records"]:
            return None
        return (
            dict(state["metadata"]),
            [dict(row) for row in state["records"]],
            json.loads(json.dumps(state["insights"] or {})),
            state["filename"],
        )


def _empty_dashboard():
    return {
        "is_empty": True, "selected_bank": "All", "available_statements": [],
        "insights": {},
        "tab_counts": {"all": 0, "salary": 0, "loans": 0, "trading": 0, "churn": 0, "bounce": 0},
        "chart_cashflow": {"labels": [], "inflows": [], "outflows": [], "net": []},
        "chart_categories": {"labels": [], "values": [], "counts": {}},
        "chart_balance": [], "total_transactions": 0,
    }


@app.get("/api/health")
def health(request: Request, response: Response):
    sid = _session_id(request, response)
    snap = _snapshot(sid)
    return {"status": "ok", "has_statement": bool(snap), "storage": "in-memory"}


@app.get("/api/statements")
def get_statements(request: Request, response: Response):
    sid = _session_id(request, response)
    snap = _snapshot(sid)
    if not snap:
        return {"statements": []}
    meta, _, insights, filename = snap
    return {"statements": [{
        "bank": meta.get("bank"), "borrower_name": meta.get("borrower_name"),
        "account_no": meta.get("account_no"), "closing_balance": meta.get("closing_balance", 0),
        "abb": insights.get("abb_metrics", {}).get("overall_abb", 0), "filename": filename,
    }]}


@app.get("/api/dashboard")
def get_dashboard(request: Request, response: Response, bank: Optional[str] = None):
    sid = _session_id(request, response)
    snap = _snapshot(sid)
    if not snap:
        return _empty_dashboard()
    metadata, records, insights, filename = snap
    if bank and bank.lower() != "all" and bank not in (metadata.get("bank"), metadata.get("account_no")):
        return _empty_dashboard()

    df = pd.DataFrame(records)
    if df.empty:
        return _empty_dashboard()
    df["credit"] = pd.to_numeric(df.get("credit", 0), errors="coerce").fillna(0.0)
    df["debit"] = pd.to_numeric(df.get("debit", 0), errors="coerce").fillna(0.0)
    df["month"] = df["iso_date"].astype(str).str[:7]
    monthly = df[df["month"].str.len() == 7].groupby("month")[["credit", "debit"]].sum().reset_index().sort_values("month")
    monthly["net"] = monthly["credit"] - monthly["debit"]
    cat_summary = df.groupby("category")[["debit", "credit"]].sum().reset_index()
    df_sorted = df.sort_values(["iso_date"])
    df_sorted = df_sorted.drop_duplicates(subset=["iso_date", "txn_date", "balance"], keep="last")
    step = max(1, len(df_sorted) // 40)
    sampled_balance = df_sorted.iloc[::step][["txn_date", "iso_date", "balance", "bank"]].to_dict("records")
    tab_counts = {
        "all": len(records),
        "salary": int(df["category"].astype(str).str.contains("SALARY|BUSINESS_INFLOW", case=False, na=False).sum()),
        "loans": int(df["category"].astype(str).str.contains("LOAN|EMI", case=False, na=False).sum()),
        "trading": int(df["category"].astype(str).str.contains("STOCK|TRADING", case=False, na=False).sum()),
        "churn": int(df["category"].astype(str).str.contains("CC_PAYMENT|WALLET_TOPUP|CC_WALLET_CHURN", case=False, na=False).sum()),
        "bounce": int(df["category"].astype(str).str.contains("BOUNCE", case=False, na=False).sum()),
    }
    statement_summary = [{
        "bank": metadata.get("bank"), "borrower_name": metadata.get("borrower_name"),
        "account_no": metadata.get("account_no"), "abb": insights.get("abb_metrics", {}).get("overall_abb", 0),
        "closing_balance": metadata.get("closing_balance", 0), "filename": filename,
    }]
    return {
        "is_empty": False, "selected_bank": bank or "All", "available_statements": statement_summary,
        "insights": insights, "tab_counts": tab_counts,
        "chart_cashflow": {
            "labels": monthly["month"].tolist(),
            "inflows": [round(float(v), 2) for v in monthly["credit"].tolist()],
            "outflows": [round(float(v), 2) for v in monthly["debit"].tolist()],
            "net": [round(float(v), 2) for v in monthly["net"].tolist()],
        },
        "chart_categories": {
            "labels": cat_summary["category"].tolist(),
            "values": [round(float(d + c), 2) for d, c in zip(cat_summary["debit"].tolist(), cat_summary["credit"].tolist())],
            "counts": df["category"].value_counts().to_dict(),
        },
        "chart_balance": sampled_balance, "total_transactions": len(records),
    }


@app.get("/api/transactions")
def get_transactions(
    request: Request, response: Response,
    bank: Optional[str] = None, tab: str = "all", search: Optional[str] = None,
    start_date: Optional[str] = None, end_date: Optional[str] = None,
    category: Optional[str] = None, min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    sort_by: str = "iso_date", sort_dir: str = "desc",
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=500),
):
    sid = _session_id(request, response)
    snap = _snapshot(sid)
    if not snap:
        return {"transactions": [], "total": 0, "page": page, "limit": limit, "total_pages": 1}
    metadata, records, _, _ = snap
    if bank and bank.lower() != "all" and bank not in (metadata.get("bank"), metadata.get("account_no")):
        records = []
    rows = list(records)
    category_text = lambda r: str(r.get("category") or "").upper()
    if tab == "salary": rows = [r for r in rows if "SALARY" in category_text(r) or "BUSINESS_INFLOW" in category_text(r)]
    elif tab == "loans": rows = [r for r in rows if "LOAN" in category_text(r) or "EMI" in category_text(r)]
    elif tab == "trading": rows = [r for r in rows if "STOCK" in category_text(r) or "TRADING" in category_text(r)]
    elif tab in ("churn", "credit_cards", "wallets"): rows = [r for r in rows if any(k in category_text(r) for k in ("CC_PAYMENT", "WALLET_TOPUP", "CC_WALLET_CHURN"))]
    elif tab == "bounce": rows = [r for r in rows if "BOUNCE" in category_text(r)]
    if category and category.lower() != "all": rows = [r for r in rows if r.get("category") == category]
    if search:
        needle = search.strip().casefold()
        rows = [r for r in rows if needle in str(r.get("narration", "")).casefold() or needle in str(r.get("txn_date", "")).casefold() or needle in str(r.get("bank", "")).casefold()]
    if start_date: rows = [r for r in rows if str(r.get("iso_date", "")) >= start_date]
    if end_date: rows = [r for r in rows if str(r.get("iso_date", "")) <= end_date]
    if min_amount is not None: rows = [r for r in rows if max(float(r.get("debit") or 0), float(r.get("credit") or 0)) >= min_amount]
    if max_amount is not None: rows = [r for r in rows if max(float(r.get("debit") or 0), float(r.get("credit") or 0)) <= max_amount]
    valid_sorts = {"iso_date", "debit", "credit", "balance", "txn_date"}
    sort_col = sort_by if sort_by in valid_sorts else "iso_date"
    rows.sort(key=lambda r: (r.get(sort_col) or "", r.get("txn_date") or ""), reverse=(sort_dir.lower() != "asc"))
    total = len(rows)
    total_pages = max(1, (total + limit - 1) // limit)
    start = (page - 1) * limit
    return {"transactions": rows[start:start + limit], "total": total, "page": page, "limit": limit, "total_pages": total_pages}


@app.post("/api/reset")
def reset_session(request: Request, response: Response):
    sid = _session_id(request, response)
    _clear_statement(sid)
    return {"success": True, "message": "Your current in-memory statement was cleared."}


@app.post("/api/upload")
async def upload_statement(request: Request, response: Response, file: UploadFile = File(...), password: Optional[str] = Form(None)):
    sid = _session_id(request, response)
    filename = file.filename or "bank_statement.pdf"
    content = await file.read()
    try:
        detection, records, metadata, insights = await run_in_threadpool(_parse_statement, content, password, filename)
        _store_statement(sid, metadata, records, insights, filename)
        return {
            "success": True, "bank": metadata.get("bank"),
            "borrower_name": metadata.get("borrower_name"),
            "transactions_parsed": len(records), "total_parsed": len(records),
            "reconciled": insights.get("reconciliation", {}).get("is_reconciled", False),
            "reconciliation": insights.get("reconciliation", {}),
            "layout_detected": detection.get("layout", "Auto-Detect"),
        }
    except PermissionError as exc:
        return {"success": False, "needs_password": True, "detected_bank": "Protected Statement", "filename": filename, "message": str(exc)}
    except ValueError as exc:
        message = str(exc)
        if "Incorrect PDF password" in message:
            return {"success": False, "needs_password": True, "detected_bank": "Protected Statement", "filename": filename, "error": message}
        raise HTTPException(status_code=422, detail=message)
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Statement processing failed. Please try again or use another PDF.") from exc
    finally:
        await file.close()


def _export_snapshot(bank: Optional[str], sid: str):
    snap = _snapshot(sid)
    if not snap:
        raise HTTPException(status_code=404, detail="No statement available. Upload a PDF first.")
    metadata, records, insights, filename = snap
    if bank and bank.lower() != "all" and bank not in (metadata.get("bank"), metadata.get("account_no")):
        raise HTTPException(status_code=404, detail="Requested statement is not available.")
    return metadata, records, insights, filename


@app.get("/api/export/excel")
def export_excel(request: Request, response: Response, bank: Optional[str] = None):
    sid = _session_id(request, response)
    metadata, records, insights, filename = _export_snapshot(bank, sid)
    stream = create_excel_report(insights, records, bank_filter=metadata.get("bank", "Statement"))
    out_name = re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(filename)[0]) or "Bank_Statement"
    return Response(content=stream.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{out_name}_Analysis.xlsx"'})


@app.get("/api/export/json")
def export_json(request: Request, response: Response, bank: Optional[str] = None):
    sid = _session_id(request, response)
    metadata, records, insights, filename = _export_snapshot(bank, sid)
    payload = create_los_json(insights, records, bank_filter=metadata.get("bank", "Statement"))
    out_name = re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(filename)[0]) or "Bank_Statement"
    return Response(content=payload, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{out_name}_LOS.json"'})


@app.get("/", response_class=HTMLResponse)
def index_page():
    candidates = [os.path.join(TEMPLATES_DIR, "index.html"), os.path.join(BASE_DIR, "index.html")]
    for index_path in candidates:
        if os.path.isfile(index_path):
            with open(index_path, "r", encoding="utf-8") as handle:
                return handle.read()
    return HTMLResponse("<h1>FinTech Bank Statement Analyzer</h1>", status_code=200)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 8000)))
