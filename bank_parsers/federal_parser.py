"""Dedicated parser for Federal Bank e-statements (10-column table format)."""
from __future__ import annotations

import re
from io import BytesIO

import pdfplumber
import pymupdf

from .common import (
    clean_amount,
    parse_iso_date,
    categorize_transaction,
    _fill_period_from_records,
)


def parse_federal_pdf(pdf_source, password=None):
    date_regex = re.compile(r'^\d{2}-[A-Z]{3}-\d{4}')
    extracted_rows = []
    metadata = {
        "bank": "Federal Bank", "borrower_name": "Federal Bank Account Holder", "account_no": "",
        "branch": "Federal Bank Branch", "sol_id": "", "ifsc": "FDRL0000000", "cust_id": "",
        "email": "", "mobile": "", "account_type": "Savings Account", "period": "",
        "opening_balance": 0.0, "closing_balance": 0.0, "total_withdrawals": 0.0, "total_deposits": 0.0
    }
    try:
        doc = pymupdf.open(stream=pdf_source if isinstance(pdf_source, bytes) else None, 
                           filename=pdf_source if isinstance(pdf_source, str) else None)
        if doc.is_encrypted and password:
            doc.authenticate(password)
        first_page_text = doc[0].get_text()
        name_m = re.search(r'Name\s*:\s*([^\n\r]+)', first_page_text)
        if name_m:
            metadata["borrower_name"] = name_m.group(1).strip()
        acc_m = re.search(r'Account Number\s*:\s*([0-9]+)', first_page_text)
        if acc_m:
            metadata["account_no"] = acc_m.group(1).strip()
        branch_m = re.search(r'Branch Name\s*:\s*([^\n\r]+)', first_page_text)
        if branch_m:
            metadata["branch"] = branch_m.group(1).strip()
        ifsc_m = re.search(r'IFSC\s*:\s*([A-Za-z0-9]+)', first_page_text)
        if ifsc_m:
            metadata["ifsc"] = ifsc_m.group(1).strip()
        doc.close()
    except Exception:
        pass

    pdf_target = BytesIO(pdf_source) if isinstance(pdf_source, bytes) else pdf_source
    with pdfplumber.open(pdf_target, password=password) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row or not any(row):
                        continue
                    first_col = (row[0] or "").strip()
                    if date_regex.match(first_col):
                        extracted_rows.append(row)
                    elif extracted_rows and len(row) > 2:
                        extra = " ".join([str(c).strip() for c in row if c])
                        extracted_rows[-1][2] = (extracted_rows[-1][2] or "") + " " + extra

    records = []
    for r in extracted_rows:
        if len(r) < 9:
            continue
        txn_date = r[0].strip()
        iso_date = parse_iso_date(txn_date)
        narration = (r[2] or "").replace('\n', ' ').strip()
        debit = clean_amount(r[6])
        credit = clean_amount(r[7])
        balance = clean_amount(r[8])
        category = categorize_transaction(narration, debit, credit)
        records.append({
            "bank": "Federal Bank",
            "account_no": metadata["account_no"],
            "txn_date": txn_date,
            "iso_date": iso_date,
            "narration": narration,
            "debit": debit,
            "credit": credit,
            "balance": balance,
            "category": category
        })

    if records:
        first = records[0]
        metadata["opening_balance"] = round(first["balance"] + first["debit"] - first["credit"], 2)
        metadata["closing_balance"] = records[-1]["balance"]
        metadata["total_withdrawals"] = round(sum(r["debit"] for r in records), 2)
        metadata["total_deposits"] = round(sum(r["credit"] for r in records), 2)
        _fill_period_from_records(metadata, records)
    return records, metadata
