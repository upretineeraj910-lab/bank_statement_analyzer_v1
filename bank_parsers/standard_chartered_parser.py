"""Dedicated parser for Standard Chartered Bank e-statements (7-column layout)."""
from __future__ import annotations

import re
from io import BytesIO

import pdfplumber
import pymupdf

from .common import (
    clean_amount,
    is_date,
    parse_iso_date,
    categorize_transaction,
    _fill_period_from_records,
)


def parse_standard_chartered_pdf(pdf_source, password=None):
    metadata = {
        "bank": "Standard Chartered Bank", "borrower_name": "Account Holder", "account_no": "",
        "branch": "Main Branch", "sol_id": "", "ifsc": "SCBL0000000", "cust_id": "", "email": "",
        "mobile": "", "account_type": "Smart Banking Savings Account", "period": "",
        "opening_balance": 0.0, "closing_balance": 0.0, "total_withdrawals": 0.0, "total_deposits": 0.0
    }
    try:
        doc = pymupdf.open(stream=pdf_source if isinstance(pdf_source, bytes) else None,
                           filename=pdf_source if isinstance(pdf_source, str) else None)
        if doc.is_encrypted and password:
            doc.authenticate(password)
        p0_text = doc[0].get_text()
        name_m = (re.search(r'^(?:MR|MS|MRS)\.?\s+([A-Za-z\s\.]+?)(?:\r?\n|$)', p0_text, re.M | re.I) or
                  re.search(r'(?:MR|MS|MRS)\.?\s+([A-Za-z\s]+?)\s+BRANCH', p0_text, re.I))
        if name_m:
            cand = name_m.group(1).strip()
            cand = re.sub(r'\s+BRANCH.*$', '', cand, flags=re.I).strip()
            if cand and not any(k in cand.upper() for k in ["BANK", "STATEMENT", "ACCOUNT", "PERIOD", "PAGE"]):
                metadata["borrower_name"] = f"MR {cand}" if not cand.upper().startswith(("MR", "MS", "MRS")) else cand
        acc_m = re.search(r'ACCOUNT\s*NO\s*:\s*([0-9]+)', p0_text, re.I)
        if acc_m:
            metadata["account_no"] = acc_m.group(1).strip()
        br_m = re.search(r'BRANCH\s*:\s*([^\n\r]+)', p0_text, re.I)
        if br_m:
            metadata["branch"] = br_m.group(1).strip()
        ifsc_m = re.search(r'IFSC\s*:\s*([A-Za-z0-9]+)', p0_text, re.I)
        if ifsc_m:
            metadata["ifsc"] = ifsc_m.group(1).strip()
        date_m = re.search(r'STATEMENT\s*DATE\s*:\s*([^\n\r]+)', p0_text, re.I)
        if date_m:
            metadata["period"] = date_m.group(1).strip()
        doc.close()
    except Exception as e:
        print(f"[SC Metadata Warning] {e}")

    records = []
    try:
        pdf_target = BytesIO(pdf_source) if isinstance(pdf_source, bytes) else pdf_source
        with pdfplumber.open(pdf_target, password=password) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                if not tables:
                    continue
                for table in tables:
                    for row in table:
                        if not row or not any(row):
                            continue
                        row_strs = [str(c or '').strip() for c in row]
                        if len(row_strs) < 6:
                            continue
                        row_lower = [c.lower() for c in row_strs]
                        if 'deposit' in row_lower or 'withdrawal' in row_lower or 'total' in row_lower:
                            continue
                        row_full = " ".join(row_strs).upper()
                        if 'BALANCE FORWARD' in row_full or 'B/F' in row_full:
                            bal_str = row_strs[-1] or row_strs[-2]
                            metadata['opening_balance'] = clean_amount(bal_str)
                            continue

                        date_val = row_strs[0].strip()
                        if not date_val or not is_date(date_val):
                            continue

                        desc = row_strs[2].replace('\n', ' ').strip()
                        chq = row_strs[3].strip() if len(row_strs) > 3 else ""
                        if chq and chq not in desc:
                            desc = f"{desc} Ref:{chq}"

                        dep = clean_amount(row_strs[4])
                        wdr = clean_amount(row_strs[5])
                        bal = clean_amount(row_strs[6]) if len(row_strs) > 6 else 0.0

                        category = categorize_transaction(desc, wdr, dep)
                        records.append({
                            "bank": metadata["bank"],
                            "account_no": metadata.get("account_no", ""),
                            "txn_date": date_val,
                            "iso_date": parse_iso_date(date_val),
                            "narration": desc,
                            "debit": wdr,
                            "credit": dep,
                            "balance": bal,
                            "category": category
                        })
    except Exception as e:
        print(f"[Standard Chartered Parser Error] {e}")

    if records:
        if metadata["opening_balance"] == 0.0 and len(records) > 0:
            metadata["opening_balance"] = round(records[0]["balance"] - records[0]["credit"] + records[0]["debit"], 2)
        metadata["closing_balance"] = records[-1]["balance"]
        metadata["total_withdrawals"] = round(sum(r["debit"] for r in records), 2)
        metadata["total_deposits"] = round(sum(r["credit"] for r in records), 2)
        _fill_period_from_records(metadata, records)

    return records, metadata
