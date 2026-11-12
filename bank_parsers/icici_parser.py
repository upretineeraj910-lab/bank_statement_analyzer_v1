"""Dedicated parser for ICICI Bank text-based e-statements.

This ICICI PDF template is extracted from positioned PDF text (PyMuPDF), not
pdfplumber tables: its transaction rows are laid out as text and table extraction
returns only the column headings for the supplied samples.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Optional

import fitz

_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_AMOUNT_RE = re.compile(r"^\d[\d,]*\.\d{2}$")
_FOOTER_MARKERS = {
    "sincerly", "sincerely", "legends", "www.icici.bank.in", "never",
    "share", "this", "team", "dial",
}


def _date_iso(value: str) -> str:
    try:
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return ""


def _default_category(narration: str, debit: float, credit: float) -> str:
    text = narration.upper()
    if any(x in text for x in ("EMI", "LOAN", "NACH", "MANDATE", "ACH/")):
        return "LOAN_EMI"
    if any(x in text for x in ("SALARY", "PAYROLL")):
        return "SALARY" if credit else "OTHER"
    if any(x in text for x in ("ATM", "CASH", "NFS/CASH")):
        return "CASH_WITHDRAWAL" if debit else "OTHER"
    if any(x in text for x in ("NEFT", "RTGS", "IMPS", "UPI", "INF/")):
        return "TRANSFER"
    return "OTHER"


def parse_icici_pdf(pdf_source, password=None,
                    categorize_fn: Optional[Callable] = None,
                    date_parser: Optional[Callable] = None):
    metadata = {
        "bank": "ICICI Bank", "borrower_name": "ICICI Account Holder", "account_no": "",
        "branch": "ICICI Bank Branch", "sol_id": "", "ifsc": "", "cust_id": "",
        "email": "", "mobile": "", "account_type": "Savings Account", "period": "",
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
                raise ValueError("ICICI Bank PDF is password protected or password is incorrect")

        full_text = "\n".join(page.get_text("text") for page in doc)
        acc = re.search(
            r"(?:Saving|Savings|Current|Salary)\s+Account\s+no\.?\s*(\d{9,18})",
            full_text, re.I,
        )
        if acc:
            metadata["account_no"] = acc.group(1)
        period = re.search(
            r"for\s+the\s+period\s+([^\r\n]+)", full_text, re.I
        )
        if period:
            metadata["period"] = period.group(1).strip()

        for page in doc:
            words = page.get_text("words")
            date_words = [w for w in words if _DATE_RE.fullmatch(w[4].strip())]
            date_words.sort(key=lambda w: (w[1], w[0]))
            if not date_words:
                continue

            # Footer begins after the final transaction on a page. Keep it out of
            # the final narration without truncating long transaction references.
            footer_y = min(
                (w[1] for w in words if w[4].strip().lower().strip(" ,.:;") in _FOOTER_MARKERS),
                default=page.rect.height + 1,
            )

            for idx, date_word in enumerate(date_words):
                y = date_word[1]
                next_y = date_words[idx + 1][1] if idx + 1 < len(date_words) else footer_y
                if next_y <= y:
                    next_y = page.rect.height + 1

                # In this ICICI layout, the withdrawal/deposit amount and balance
                # share the same baseline as the transaction date. Their X position
                # identifies the original statement column.
                row_amounts = [
                    w for w in words
                    if abs(w[1] - y) <= 2.5
                    and _AMOUNT_RE.fullmatch(w[4].replace(",", ""))
                    and w[0] > 350
                ]
                debit = credit = balance = 0.0
                for word in row_amounts:
                    amount = float(word[4].replace(",", ""))
                    x0 = word[0]
                    if x0 >= 520:
                        balance = amount
                    elif x0 >= 465:
                        credit = amount
                    else:
                        debit = amount

                if balance <= 0 or (debit <= 0 and credit <= 0):
                    continue

                narration_parts = []
                for word in words:
                    x0, y0, _x1, _y1, value = word[:5]
                    value = value.strip()
                    if y0 < y - 1 or y0 >= next_y - 1:
                        continue
                    if _DATE_RE.fullmatch(value) or _AMOUNT_RE.fullmatch(value.replace(",", "")):
                        continue
                    # The serial number is printed at the far left of the date row.
                    if abs(y0 - y) <= 2.5 and x0 < 75 and value.isdigit():
                        continue
                    if y0 >= footer_y:
                        continue
                    if value.lower().strip(" ,.:;") in {"www.icici.bank.in", "dial", "sincerely", "sincerly", "legends"}:
                        continue
                    narration_parts.append((y0, x0, value))

                narration = " ".join(
                    item[2] for item in sorted(narration_parts, key=lambda z: (z[0], z[1]))
                )
                narration = re.sub(r"\s+", " ", narration).strip()
                if not narration:
                    narration = "ICICI Bank transaction"

                txn_date = date_word[4].strip()
                iso_date = date_parser(txn_date) if date_parser else _date_iso(txn_date)
                category = categorize_fn(narration, debit, credit) if categorize_fn else _default_category(narration, debit, credit)
                records.append({
                    "bank": "ICICI Bank",
                    "account_no": metadata["account_no"],
                    "txn_date": txn_date,
                    "iso_date": iso_date,
                    "narration": narration,
                    "debit": round(debit, 2),
                    "credit": round(credit, 2),
                    "balance": round(balance, 2),
                    "category": category,
                })

        if records:
            first = records[0]
            metadata["opening_balance"] = round(first["balance"] + first["debit"] - first["credit"], 2)
            metadata["closing_balance"] = records[-1]["balance"]
            metadata["total_withdrawals"] = round(sum(r["debit"] for r in records), 2)
            metadata["total_deposits"] = round(sum(r["credit"] for r in records), 2)
            if not metadata["period"]:
                metadata["period"] = f"{records[0]['txn_date']} to {records[-1]['txn_date']}"
    finally:
        if doc:
            doc.close()
    return records, metadata
