"""Dedicated universal/generic fallback parser for bank statements."""
from __future__ import annotations

import re
from io import BytesIO

import pdfplumber
import pymupdf

from .common import (
    clean_amount,
    is_date,
    is_amount,
    parse_iso_date,
    categorize_transaction,
    _fill_period_from_records,
)

BANK_IFSC_MAP = {
    "SCBL": "Standard Chartered Bank", "ICIC": "ICICI Bank", "HDFC": "HDFC Bank",
    "SBIN": "State Bank of India", "UTIB": "Axis Bank", "FDRL": "Federal Bank",
    "IDFB": "IDFC FIRST Bank", "KKBK": "Kotak Mahindra Bank", "PUNB": "Punjab National Bank",
    "BARB": "Bank of Baroda", "CNRB": "Canara Bank", "UBIN": "Union Bank of India",
    "INDB": "IndusInd Bank", "YESB": "Yes Bank", "BKID": "Bank of India",
    "CBIN": "Central Bank of India", "RATN": "RBL Bank"
}


def parse_universal_pdf(pdf_source, password=None, detected_bank=None):
    metadata = {
        "bank": detected_bank or "General Bank Statement", "borrower_name": "Account Holder",
        "account_no": "", "branch": "Main Branch", "sol_id": "", "ifsc": "", "cust_id": "",
        "email": "", "mobile": "", "account_type": "Savings / Current Account", "period": "",
        "opening_balance": 0.0, "closing_balance": 0.0, "total_withdrawals": 0.0, "total_deposits": 0.0
    }

    p0_text = ""
    header_text = ""
    try:
        doc = pymupdf.open(stream=pdf_source if isinstance(pdf_source, bytes) else None,
                           filename=pdf_source if isinstance(pdf_source, str) else None)
        if doc.is_encrypted and password:
            doc.authenticate(password)

        if len(doc) > 0:
            p0_text = doc[0].get_text()
            m_split = re.search(r'\n\s*(?:Date|Txn\s*Date|Tran\s*Date|Value\s*Date|Description|Particulars)\s*[\n\r]', p0_text, re.I)
            header_text = p0_text[:m_split.start()] if m_split else p0_text[:2000]
        doc.close()
    except Exception as e:
        print(f"Universal Metadata Error: {e}")

    ifsc_m = (re.search(r'IFSC\s*(?:CODE)?\s*[:\.]?\s*([A-Za-z]{4}[A-Za-z0-9]{7})', header_text, re.I) or
              re.search(r'\b([A-Za-z]{4}0[A-Za-z0-9]{6})\b', header_text))
    if ifsc_m:
        ifsc_code = ifsc_m.group(1).strip().upper()
        metadata["ifsc"] = ifsc_code
        prefix = ifsc_code[:4]
        if prefix in BANK_IFSC_MAP and (not detected_bank or detected_bank in ("Generic", "General Bank Statement", "Unknown")):
            metadata["bank"] = BANK_IFSC_MAP[prefix]

    if not metadata["bank"] or metadata["bank"] in ("Generic", "General Bank Statement", "Unknown"):
        text_upper = header_text.upper()
        for pfx, bname in BANK_IFSC_MAP.items():
            if bname.upper() in text_upper:
                metadata["bank"] = bname
                break

    acc_m = (re.search(r'(?:Account|A/C|Acct)\s*(?:Number|No\.?|Num|#)?\s*[:\.]?\s*([0-9]{9,18})', header_text, re.I) or
             re.search(r'\b([0-9]{11,18})\b', header_text))
    if acc_m:
        metadata["account_no"] = acc_m.group(1).strip()

    name_m = (re.search(r'^(?:MR|MS|MRS)\.?\s+([A-Za-z\s\.]+?)(?:\r?\n|$)', header_text, re.M | re.I) or
              re.search(r'(?:Customer|Account\s*Holder|Borrower|A/C)?\s*Name\s*[:\.]?\s*([A-Za-z\s\.]+?)(?:\r?\n|$)', header_text, re.I))
    if name_m:
        cand = name_m.group(1).strip()
        cand = re.sub(r'\s+BRANCH.*$', '', cand, flags=re.I).strip()
        if cand and not any(k in cand.upper() for k in ["BANK", "STATEMENT", "BRANCH", "ACCOUNT", "PERIOD", "PAGE"]):
            metadata["borrower_name"] = f"MR {cand}" if not cand.upper().startswith(("MR", "MS", "MRS")) else cand

    records = []
    try:
        pdf_target = BytesIO(pdf_source) if isinstance(pdf_source, bytes) else pdf_source
        with pdfplumber.open(pdf_target, password=password) as pdf:
            col_date = -1
            col_narration = -1
            col_chq = -1
            col_debit = -1
            col_credit = -1
            col_balance = -1

            for page in pdf.pages:
                tables = page.extract_tables()
                if not tables:
                    tables = page.extract_tables(table_settings={"vertical_strategy": "text", "horizontal_strategy": "text"})
                if not tables:
                    continue

                for table in tables:
                    if not table or len(table) < 2:
                        continue

                    for row_idx, row in enumerate(table):
                        if not row or not any(row):
                            continue
                        row_strs = [str(c or "").strip() for c in row]
                        row_lower = [s.lower() for s in row_strs]

                        has_date_hdr = any(any(k in s for k in ["date", "txn", "tran"]) for s in row_lower)
                        has_amt_hdr = any(any(k in s for k in ["withdrawal", "deposit", "debit", "credit", "balance", "amount", "bal", "dr", "cr"]) for s in row_lower)

                        if has_amt_hdr and (has_date_hdr or row_idx == 0):
                            for idx, s in enumerate(row_lower):
                                if ("txn" in s or "tran" in s or "date" in s) and col_date == -1:
                                    col_date = idx
                                elif any(k in s for k in ["particular", "narration", "desc", "remark", "detail"]):
                                    col_narration = idx
                                elif any(k in s for k in ["cheque", "chq", "ref"]):
                                    col_chq = idx
                                elif any(k in s for k in ["withdrawal", "debit", "dr"]):
                                    col_debit = idx
                                elif any(k in s for k in ["deposit", "credit", "cr"]):
                                    col_credit = idx
                                elif "balance" in s or "bal" in s:
                                    col_balance = idx
                            continue

                        if any("total" in s for s in row_lower) or any("closing balance" in s for s in row_lower):
                            continue

                        row_text_all = " ".join(row_strs).upper()
                        if ("BALANCE FORWARD" in row_text_all or "OPENING BALANCE" in row_text_all or "B/F" in row_text_all):
                            amts = [clean_amount(c) for c in row_strs if is_amount(c)]
                            if amts:
                                metadata["opening_balance"] = amts[-1]
                                continue

                        row_date = None
                        date_idx = -1
                        if col_date >= 0 and col_date < len(row_strs) and is_date(row_strs[col_date]):
                            row_date = row_strs[col_date].split('\n')[0].strip()
                            date_idx = col_date
                        else:
                            for idx, cell in enumerate(row_strs):
                                if is_date(cell):
                                    row_date = cell.split('\n')[0].strip()
                                    date_idx = idx
                                    break

                        if not row_date:
                            if records:
                                rem_text = row_strs[col_narration] if 0 <= col_narration < len(row_strs) else " ".join([c for c in row_strs if len(c) > 3 and not is_amount(c)])
                                if rem_text and not any(k in rem_text.upper() for k in ["PAGE", "TOTAL", "OPENING"]):
                                    records[-1]["narration"] = f"{records[-1]['narration']} {rem_text}".strip()
                            continue

                        debit = 0.0
                        credit = 0.0
                        balance = 0.0

                        if col_debit >= 0 and col_credit >= 0 and col_balance >= 0 and max(col_debit, col_credit, col_balance) < len(row_strs):
                            debit = clean_amount(row_strs[col_debit])
                            credit = clean_amount(row_strs[col_credit])
                            balance = clean_amount(row_strs[col_balance])
                        else:
                            num_vals = [clean_amount(c) for idx, c in enumerate(row_strs) if idx != date_idx and is_amount(c)]
                            if len(num_vals) >= 3:
                                balance = num_vals[-1]
                                debit = num_vals[-3]
                                credit = num_vals[-2]
                            elif len(num_vals) == 2 and records:
                                balance = num_vals[-1]
                                amt = num_vals[-2]
                                diff = round(balance - records[-1]["balance"], 2)
                                credit = amt if diff > 0 else 0.0
                                debit = amt if diff <= 0 else 0.0

                        narration = row_strs[col_narration].replace('\n', ' ').strip() if 0 <= col_narration < len(row_strs) else " ".join([c for i, c in enumerate(row_strs) if i != date_idx and not is_amount(c)])
                        if col_chq >= 0 and col_chq < len(row_strs) and row_strs[col_chq].strip() and row_strs[col_chq].strip() not in narration:
                            narration = f"{narration} Ref:{row_strs[col_chq].strip()}"

                        narration = re.sub(r'\s+', ' ', narration).strip()
                        category = categorize_transaction(narration, debit, credit)

                        records.append({
                            "bank": metadata["bank"],
                            "account_no": metadata.get("account_no", ""),
                            "txn_date": row_date,
                            "iso_date": parse_iso_date(row_date),
                            "narration": narration,
                            "debit": debit,
                            "credit": credit,
                            "balance": balance,
                            "category": category
                        })
    except Exception as e:
        print(f"Universal Table Extractor error: {e}")

    if records:
        if metadata["opening_balance"] == 0.0 and len(records) > 0:
            first_diff = records[0]["credit"] - records[0]["debit"]
            metadata["opening_balance"] = round(records[0]["balance"] - first_diff, 2)
        metadata["closing_balance"] = records[-1]["balance"]
        metadata["total_withdrawals"] = round(sum(r["debit"] for r in records), 2)
        metadata["total_deposits"] = round(sum(r["credit"] for r in records), 2)
        _fill_period_from_records(metadata, records)

    return records, metadata
