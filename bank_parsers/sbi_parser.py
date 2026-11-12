"""Dedicated parser for SBI Bank (State Bank of India) e-statements (8-column standard format)."""
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


def parse_sbi_pdf(pdf_source, password=None):
    metadata = {
        "bank": "SBI Bank", "borrower_name": "SBI Account Holder", "account_no": "",
        "branch": "SBI Branch", "sol_id": "", "ifsc": "SBIN0000000", "cust_id": "",
        "email": "", "mobile": "", "account_type": "REGULAR SB CHQ-INDIVIDUALS", "period": "",
        "opening_balance": 0.0, "closing_balance": 0.0, "total_withdrawals": 0.0, "total_deposits": 0.0
    }
    
    all_text = ""
    try:
        if isinstance(pdf_source, bytes):
            doc = pymupdf.open(stream=pdf_source, filetype="pdf")
        else:
            doc = pymupdf.open(pdf_source)
        if doc.is_encrypted and password:
            doc.authenticate(password)
            
        p0_text = doc[0].get_text()
        name_m = re.search(r'Welcome:\s*\n+([^\n\r]+)', p0_text)
        if name_m:
            metadata["borrower_name"] = name_m.group(1).replace('.', '').strip()
            
        acc_m = re.search(r':Account Number\s*\n+([0-9]+)', p0_text) or re.search(r'Account Number\s*:\s*([0-9]+)', p0_text)
        if acc_m:
            metadata["account_no"] = acc_m.group(1).strip()
            
        ifsc_m = re.search(r':IFSC Code\s*\n+([A-Za-z0-9]+)', p0_text) or re.search(r'IFSC Code\s*:\s*([A-Za-z0-9]+)', p0_text)
        if ifsc_m:
            metadata["ifsc"] = ifsc_m.group(1).strip()
            
        br_m = re.search(r'Branch Name\s*:\s*([^\n\r]+)', p0_text)
        if br_m:
            metadata["branch"] = br_m.group(1).strip()
            
        cif_m = re.search(r':CIF Number\s*\n+([0-9]+)', p0_text)
        if cif_m:
            metadata["cust_id"] = cif_m.group(1).strip()
            
        email_m = re.search(r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', p0_text)
        if email_m:
            metadata["email"] = email_m.group(1).strip()
            
        per_m = re.search(r'Statement From\s*:([0-9/-]+)\s*to\s*([0-9/-]+)', p0_text)
        if per_m:
            metadata["period"] = f"{per_m.group(1)} to {per_m.group(2)}"
            
        last_page_text = doc[-1].get_text()
        sum_m = re.search(r'([\d,]+\.\d{2}CR?)\s+(\d+)\s+(\d+)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2}CR?)', last_page_text)
        if sum_m:
            metadata["opening_balance"] = clean_amount(sum_m.group(1))
            metadata["total_withdrawals"] = clean_amount(sum_m.group(4))
            metadata["total_deposits"] = clean_amount(sum_m.group(5))
            metadata["closing_balance"] = clean_amount(sum_m.group(6))
            
        for page in doc:
            all_text += "\n" + page.get_text()
        doc.close()
    except Exception as e:
        print(f"SBI Metadata error: {e}")

    lines = all_text.split('\n')
    date_pat = re.compile(r'^\d{2}[/-]\d{2}[/-]\d{4}\b')
    num_pat = re.compile(r'^-?[\d,]+\.\d{2}$')
    
    ignore_prefixes = (
        "Account Summary", "Welcome:", "Date of Statement", "Uncleared Amount",
        "Clear Balance", "+MOD Bal", "Lien", "Limit", "Monthly Avg Balance",
        "Interest Rate", "Drawing Power", "Account open Date", "Branch Code",
        "Branch Email", "Branch Phone", ":CIF Number", ":Account Number",
        ":Account Status", ":Currency", ":IFSC Code", ":Product", ":Nominee Name",
        "Statement From", "CKYCR Number", "MICR Code", "STATEMENT OF ACCOUNT",
        "State Bank of India", "Branch Name", "Txn Date", "Value Date", "Description",
        "Ref No./Cheque No.", "Debit", "Credit", "Balance", "Page no", "Please do not share",
        "If your account", "This is a computer"
    )

    txn_blocks = []
    curr_block = []
    for line in lines:
        l_str = line.strip()
        if not l_str:
            continue
        if "Statement Summary:" in l_str or "Brought Forward" in l_str:
            if curr_block:
                txn_blocks.append(curr_block)
                curr_block = []
            break
        if any(l_str.startswith(h) for h in ignore_prefixes):
            continue
        if date_pat.match(l_str):
            if curr_block:
                txn_blocks.append(curr_block)
            curr_block = [l_str]
        elif curr_block:
            curr_block.append(l_str)
            
    if curr_block:
        txn_blocks.append(curr_block)

    records = []
    prev_balance = metadata["opening_balance"]
    
    for block in txn_blocks:
        full_block_text = " ".join(block)
        tokens = full_block_text.split()
        if not tokens or not date_pat.match(tokens[0]):
            continue
            
        txn_date = tokens[0]
        num_indices = [i for i, t in enumerate(tokens) if num_pat.match(t.replace('CR', ''))]
        if len(num_indices) < 2:
            continue
            
        balance = clean_amount(tokens[num_indices[-1]])
        amt = clean_amount(tokens[num_indices[-2]])
        
        narr_tokens = tokens[2:num_indices[-2]]
        narration = " ".join(narr_tokens).strip()
        if not records and prev_balance == 0.0:
            prev_balance = _infer_opening_balance(balance, amt, narration)
            metadata["opening_balance"] = prev_balance

        diff = round(balance - prev_balance, 2)
        if diff > 0:
            credit = amt
            debit = 0.0
        else:
            debit = amt
            credit = 0.0

        prev_balance = balance
        
        records.append({
            "bank": "SBI Bank",
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
