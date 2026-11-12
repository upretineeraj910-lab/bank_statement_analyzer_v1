"""Dedicated parser for Axis Bank e-statements (7-column tabular format)."""
from __future__ import annotations

import re
from io import BytesIO

import pdfplumber
import pymupdf

from .common import (
    clean_amount,
    parse_iso_date,
    categorize_transaction,
)


def parse_axis_pdf(pdf_source, password=None):
    date_regex = re.compile(r'^\d{2}-\d{2}-\d{4}')
    extracted_rows = []
    metadata = {
        "bank": "Axis Bank", "borrower_name": "Axis Account Holder", "account_no": "",
        "branch": "Axis Bank Branch", "sol_id": "", "ifsc": "UTIB0000000", "cust_id": "",
        "email": "", "mobile": "", "account_type": "Savings Account", "period": "",
        "opening_balance": 0.0, "closing_balance": 0.0, "total_withdrawals": 0.0, "total_deposits": 0.0
    }
    
    full_text = ""
    try:
        doc = pymupdf.open(stream=pdf_source if isinstance(pdf_source, bytes) else None,
                           filename=pdf_source if isinstance(pdf_source, str) else None)
        if doc.is_encrypted and password:
            doc.authenticate(password)
            
        p0_text = doc[0].get_text()
        name_m = re.search(r'([A-Z\s]{3,35})\n\s*Joint Holder', p0_text) or re.search(r'AXIS\s*BANK\s*\n+([A-Z\s]{3,35})\n', p0_text)
        if name_m:
            metadata["borrower_name"] = name_m.group(1).replace('\n', ' ').strip()
            
        acc_m = re.search(r'Statement of Axis Account No:\s*([0-9]+)', p0_text, re.IGNORECASE) or re.search(r'Account\s*No\s*:\s*([0-9]+)', p0_text)
        if acc_m:
            metadata["account_no"] = acc_m.group(1).strip()
            
        ifsc_m = re.search(r'IFSC\s*Code\s*:\s*([A-Za-z0-9]+)', p0_text)
        if ifsc_m:
            metadata["ifsc"] = ifsc_m.group(1).strip()
            
        cust_m = re.search(r'Customer\s*ID\s*:\s*([0-9]+)', p0_text)
        if cust_m:
            metadata["cust_id"] = cust_m.group(1).strip()
            
        scheme_m = re.search(r'Scheme:\s*([^\n\r]+)', p0_text)
        if scheme_m:
            metadata["account_type"] = scheme_m.group(1).split("CKYC")[0].strip()
            
        per_m = re.search(r'period \(From:\s*([0-9-]+)\s*To:\s*([0-9-]+)\)', p0_text, re.IGNORECASE)
        if per_m:
            metadata["period"] = f"{per_m.group(1)} to {per_m.group(2)}"
            
        open_bal_m = re.search(r'OPENING\s*BALANCE\s*([\d,]+\.\d{2})', p0_text)
        if open_bal_m:
            metadata["opening_balance"] = clean_amount(open_bal_m.group(1))
            
        for page in doc:
            full_text += "\n" + page.get_text()
            
        branch_m = re.search(r'BRANCH ADDRESS - AXIS BANK LTD[,\s]+([^\n\r,]+)', full_text)
        if branch_m:
            metadata["branch"] = branch_m.group(1).strip()
            
        tot_m = re.search(r'TRANSACTION\s*TOTAL\s*([\d,]+\.\d{2})\s+([\d,]+\.\d{2})', full_text)
        if tot_m:
            metadata["total_withdrawals"] = clean_amount(tot_m.group(1))
            metadata["total_deposits"] = clean_amount(tot_m.group(2))
            
        close_m = re.search(r'CLOSING\s*BALANCE\s*([\d,]+\.\d{2})', full_text)
        if close_m:
            metadata["closing_balance"] = clean_amount(close_m.group(1))
            
        doc.close()
    except Exception as e:
        print(f"Axis metadata error: {e}")

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
        if len(r) < 6:
            continue
        txn_date = r[0].strip()
        iso_date = parse_iso_date(txn_date)
        narration = (r[2] or "").replace('\n', ' ').strip()
        if "TRANSACTION TOTAL" in narration:
            narration = narration.split("TRANSACTION TOTAL")[0].strip()
        debit = clean_amount(r[3])
        credit = clean_amount(r[4])
        balance = clean_amount(r[5])
        category = categorize_transaction(narration, debit, credit)
        
        records.append({
            "bank": "Axis Bank",
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
        if metadata["closing_balance"] == 0.0:
            metadata["closing_balance"] = records[-1]["balance"]
        if metadata["total_withdrawals"] == 0.0:
            metadata["total_withdrawals"] = round(sum(r["debit"] for r in records), 2)
            metadata["total_deposits"] = round(sum(r["credit"] for r in records), 2)
            
    return records, metadata
