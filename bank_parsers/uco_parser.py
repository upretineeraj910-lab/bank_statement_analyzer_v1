"""Dedicated parser for text-based UCO Bank e-statements.

Expected statement columns: Date | Particulars | Withdrawals | Deposits | Balance.
The parser uses the running balance delta to distinguish debit from credit because
UCO's PDF text extraction often emits the amount and balance without preserving
empty withdrawal/deposit cells.
"""
from __future__ import annotations

import re
from datetime import datetime
from io import BytesIO
from typing import Callable, Optional

import fitz  # PyMuPDF

_DATE_RE = re.compile(r"^\s*(\d{1,2}[-/][A-Za-z]{3}[-/]\d{4}|\d{1,2}[-/]\d{1,2}[-/]\d{4})\s*$", re.I)
_AMOUNT_RE = re.compile(r"(?<![A-Za-z0-9])(?:-?\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|-?\d+(?:\.\d{1,2})?)(?![A-Za-z0-9])")


def _date_iso(value: str) -> str:
    value = value.strip()
    for fmt in ("%d-%b-%Y", "%d/%b/%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _amounts(line: str) -> list[float]:
    out = []
    for match in _AMOUNT_RE.finditer(line):
        raw = match.group(0).replace(",", "")
        # Reference numbers / UTRs are not money values.
        digits = raw.lstrip("-").split(".")[0]
        if len(digits) > 10:
            continue
        try:
            out.append(round(float(raw), 2))
        except ValueError:
            continue
    return out


def _default_category(narration: str, debit: float, credit: float) -> str:
    text = narration.upper()
    if any(x in text for x in ("EMI", "LOAN", "NACH", "MANDATE")):
        return "LOAN_EMI"
    if any(x in text for x in ("SALARY", "PAYROLL")):
        return "SALARY" if credit else "OTHER"
    if any(x in text for x in ("ATM", "CASH", "CWDR")):
        return "CASH_WITHDRAWAL" if debit else "OTHER"
    if any(x in text for x in ("NEFT", "RTGS", "IMPS", "UPI", "MPAY")):
        return "TRANSFER"
    return "OTHER"


def parse_uco_pdf(pdf_source, password=None, categorize_fn: Optional[Callable] = None,
                  date_parser: Optional[Callable] = None):
    """Return (records, metadata), matching analyzer_engine's transaction schema.

    pdf_source accepts PDF bytes or a filesystem path. This parser is for selectable
    text PDFs; scanned image PDFs require OCR and are deliberately not guessed.
    """
    metadata = {
        "bank": "UCO Bank", "borrower_name": "Account Holder", "account_no": "",
        "branch": "", "sol_id": "", "ifsc": "", "cust_id": "", "email": "",
        "mobile": "", "account_type": "Savings / Current Account", "period": "",
        "opening_balance": 0.0, "closing_balance": 0.0,
        "total_withdrawals": 0.0, "total_deposits": 0.0,
    }
    records = []
    doc = None
    try:
        if isinstance(pdf_source, (bytes, bytearray)):
            doc = fitz.open(stream=bytes(pdf_source), filetype="pdf")
        else:
            doc = fitz.open(pdf_source)
        if doc.is_encrypted:
            if not password or not doc.authenticate(password):
                raise ValueError("UCO Bank PDF is password protected or password is incorrect")

        pages_text = [page.get_text("text") for page in doc]
        full_text = "\n".join(pages_text)
        if len(re.sub(r"\s+", "", full_text)) < 50:
            raise ValueError("UCO Bank PDF appears to be scanned/image-only; OCR is not supported")

        # Header metadata
        m = re.search(r"Statement\s+for\s+account\s+number\s+(\d{9,18})\s+Between\s+(.+?)\s+and\s+([^\r\n]+)", full_text, re.I)
        if m:
            metadata["account_no"] = m.group(1)
            metadata["period"] = f"{m.group(2).strip()} to {m.group(3).strip()}"
        m = re.search(r"\bIFSC\s*Code\s*\n\s*([A-Z]{4}0[A-Z0-9]{6})", full_text, re.I)
        if m:
            metadata["ifsc"] = m.group(1).upper()
        m = re.search(r"\bBranch\s*Name\s*\n\s*([^\r\n]+)", full_text, re.I)
        if m:
            metadata["branch"] = m.group(1).strip()
        m = re.search(r"\bA/c\s*Type\s*\n\s*([^\r\n]+)", full_text, re.I)
        if m:
            metadata["account_type"] = m.group(1).strip()
        m = re.search(r"\bName\s*\n\s*([^\r\n]+)", full_text, re.I)
        if m:
            metadata["borrower_name"] = m.group(1).strip()
        m = re.search(r"Opening\s+Balance\s*\n\s*([\d,]+(?:\.\d{1,2})?)", full_text, re.I)
        if m:
            metadata["opening_balance"] = float(m.group(1).replace(",", ""))

        current = None
        for page_text in pages_text:
            for raw in page_text.splitlines():
                line = re.sub(r"\s+", " ", raw).strip()
                if not line:
                    continue
                # Ignore table headings and footer boilerplate.
                if re.match(r"^(Date|Particulars|Withdrawals|Deposits|Balance)$", line, re.I):
                    continue
                if line.lower().startswith(("uco bank ,regd address", "*this is an auto generated", "closing balance")):
                    if current:
                        records.append(current)
                        current = None
                    continue
                if re.match(r"^Opening Balance$", line, re.I):
                    continue
                if _DATE_RE.match(line):
                    if current:
                        records.append(current)
                    current = {"txn_date": line, "iso_date": _date_iso(line), "narration_parts": [], "money": []}
                    continue
                if current is None:
                    continue
                # Monetary cells are emitted as separate lines by this UCO template.
                vals = _amounts(line)
                if vals and re.fullmatch(r"[\d,]+(?:\.\d{1,2})?", line):
                    current["money"].extend(vals)
                else:
                    # Keep transaction narration/reference lines, but never carry page furniture.
                    if not re.search(r"\b(?:page\s+\d+|statement\s+for\s+account)\b", line, re.I):
                        current["narration_parts"].append(line)
        if current:
            records.append(current)

        parsed = []
        previous_balance = metadata["opening_balance"]
        for item in records:
            money = item["money"]
            if len(money) < 2:
                continue
            balance = money[-1]
            amount_candidates = money[:-1]
            amount = amount_candidates[-1] if amount_candidates else 0.0
            delta = round(balance - previous_balance, 2)
            # A valid row must reconcile its amount with the change in running balance.
            if abs(delta) < 0.005:
                debit = credit = 0.0
            elif abs(abs(delta) - amount) <= 0.02:
                debit = amount if delta < 0 else 0.0
                credit = amount if delta > 0 else 0.0
            else:
                # Reject ambiguous rows instead of guessing debit/credit.
                continue
            narration = re.sub(r"\s+", " ", " ".join(item["narration_parts"])).strip()
            if not narration:
                narration = "UCO Bank transaction"
            category = (categorize_fn(narration, debit, credit) if categorize_fn else
                        _default_category(narration, debit, credit))
            iso = date_parser(item["txn_date"]) if date_parser else item["iso_date"]
            parsed.append({
                "bank": "UCO Bank", "account_no": metadata["account_no"],
                "txn_date": item["txn_date"], "iso_date": iso,
                "narration": narration, "debit": debit, "credit": credit,
                "balance": balance, "category": category,
            })
            previous_balance = balance

        if parsed:
            metadata["closing_balance"] = parsed[-1]["balance"]
            metadata["total_withdrawals"] = round(sum(x["debit"] for x in parsed), 2)
            metadata["total_deposits"] = round(sum(x["credit"] for x in parsed), 2)
            if metadata["opening_balance"] == 0:
                metadata["opening_balance"] = round(parsed[0]["balance"] - parsed[0]["credit"] + parsed[0]["debit"], 2)
        return parsed, metadata
    finally:
        if doc is not None:
            doc.close()
