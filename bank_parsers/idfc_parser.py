"""Dedicated parser for IDFC FIRST Bank e-statements (7-column tabular format)."""
from __future__ import annotations

import re
import pymupdf

from .common import (
    clean_amount,
    parse_iso_date,
    categorize_transaction,
    _infer_opening_balance,
    _fill_period_from_records,
)


def parse_idfc_pdf(pdf_source, password=None):
    metadata = {
        "bank": "IDFC FIRST Bank", "borrower_name": "IDFC FIRST Account Holder", "account_no": "",
        "branch": "IDFC FIRST Branch", "sol_id": "", "ifsc": "IDFB0000000", "cust_id": "",
        "email": "", "mobile": "", "account_type": "Savings Account", "period": "",
        "opening_balance": 0.0, "closing_balance": 0.0, "total_withdrawals": 0.0, "total_deposits": 0.0
    }
    
    full_text = ""
    try:
        doc = pymupdf.open(stream=pdf_source if isinstance(pdf_source, bytes) else None,
                           filename=pdf_source if isinstance(pdf_source, str) else None)
        if doc.is_encrypted and password:
            doc.authenticate(password)
            
        p0 = doc[0].get_text()
        name_m = re.search(r'CUSTOMER\s*NAME\s*:\s*([^\n\r]+)', p0)
        if name_m:
            metadata["borrower_name"] = name_m.group(1).replace('\n', ' ').strip()
        acc_m = re.search(r'ACCOUNT\s*NO\s*:\s*([0-9]+)', p0)
        if acc_m:
            metadata["account_no"] = acc_m.group(1).strip()
        ifsc_m = re.search(r'IFSC\s*:\s*([A-Za-z0-9]+)', p0)
        if ifsc_m:
            metadata["ifsc"] = ifsc_m.group(1).strip()
            
        sum_m = re.search(r'([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})', p0)
        if sum_m:
            metadata["opening_balance"] = clean_amount(sum_m.group(1))
            metadata["total_withdrawals"] = clean_amount(sum_m.group(2))
            metadata["total_deposits"] = clean_amount(sum_m.group(3))
            metadata["closing_balance"] = clean_amount(sum_m.group(4))
            
        for page in doc:
            full_text += "\n" + page.get_text()
        doc.close()
    except Exception:
        pass

    pattern = re.compile(
        r'(\d{2}-[A-Za-z]{3}-\d{4})\s+(\d{2}-[A-Za-z]{3}-\d{4})\s+(.*?)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})',
        re.DOTALL
    )
    
    records = []
    prev_balance = metadata["opening_balance"]
    
    for match in pattern.finditer(full_text):
        txn_date = match.group(1).strip()
        raw_narration = match.group(3).replace('\n', ' ').strip()
        raw_amt = clean_amount(match.group(4))
        balance = clean_amount(match.group(5))
        
        narration = re.sub(r'\s+', ' ', raw_narration)
        if not records and prev_balance == 0.0:
            prev_balance = _infer_opening_balance(balance, raw_amt, narration)
            metadata["opening_balance"] = prev_balance
        diff = round(balance - prev_balance, 2)
        if diff > 0:
            credit = raw_amt
            debit = 0.0
        else:
            debit = raw_amt
            credit = 0.0
            
        prev_balance = balance
        records.append({
            "bank": "IDFC FIRST Bank",
            "account_no": metadata["account_no"],
            "txn_date": txn_date,
            "iso_date": parse_iso_date(txn_date),
            "narration": narration,
            "debit": debit,
            "credit": credit,
            "balance": balance,
            "category": categorize_transaction(narration, debit, credit)
        })

    if records:
        if metadata["closing_balance"] == 0.0:
            metadata["closing_balance"] = records[-1]["balance"]
        if metadata["total_withdrawals"] == 0.0:
            metadata["total_withdrawals"] = round(sum(r["debit"] for r in records), 2)
            metadata["total_deposits"] = round(sum(r["credit"] for r in records), 2)
        _fill_period_from_records(metadata, records)

    return records, metadata
