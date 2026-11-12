"""Dedicated parser for HDFC Bank e-statements (7-column text/coordinate format)."""
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


def parse_hdfc_pdf(pdf_source, password=None, _raw_lines=False):
    metadata = {
        "bank": "HDFC Bank", "borrower_name": "HDFC Account Holder", "account_no": "",
        "branch": "HDFC Branch", "sol_id": "", "ifsc": "HDFC0000000", "cust_id": "", "email": "",
        "mobile": "", "account_type": "Savings Account", "period": "",
        "opening_balance": 0.0, "closing_balance": 0.0, "total_withdrawals": 0.0, "total_deposits": 0.0
    }

    full_text = ""
    page_texts = []
    try:
        if isinstance(pdf_source, bytes):
            doc = pymupdf.open(stream=pdf_source, filetype="pdf")
        else:
            doc = pymupdf.open(pdf_source)

        if doc.is_encrypted and password:
            doc.authenticate(password)

        p0_text = doc[0].get_text()

        name_m = (re.search(r'(?:MR|MS|MRS)\.?\s*\n+([A-Z\s]{3,40})\n', p0_text) or 
                  re.search(r'(?:MR|MS|MRS)\.?\s+([A-Z\s]{3,40})\n', p0_text) or
                  re.search(r'Name\s*[:\.]?\s*([A-Za-z\s\.]+)\n', p0_text, re.I))
        if name_m:
            metadata["borrower_name"] = name_m.group(1).replace('\n', ' ').strip()

        acc_m = re.search(r'Account\s*No\s*:\s*([0-9]+)', p0_text)
        if acc_m:
            metadata["account_no"] = acc_m.group(1).strip()

        ifsc_m = re.search(r'RTGS/NEFT\s*IFSC\s*:\s*([A-Za-z0-9]+)', p0_text)
        if ifsc_m:
            metadata["ifsc"] = ifsc_m.group(1).strip()

        br_m = re.search(r'Account\s*Branch\s*:\s*([^\n\r]+)', p0_text)
        if br_m:
            metadata["branch"] = br_m.group(1).strip()

        cust_m = re.search(r'Cust\s*ID\s*:\s*([0-9]+)', p0_text)
        if cust_m:
            metadata["cust_id"] = cust_m.group(1).strip()

        email_m = re.search(r'Email\s*:\s*([^\n\r\s]+)', p0_text)
        if email_m:
            metadata["email"] = email_m.group(1).strip()

        per_m = re.search(r'From\s*:\s*([0-9/]+)\s*To\s*:\s*([0-9/]+)', p0_text, re.IGNORECASE)
        if per_m:
            metadata["period"] = f"{per_m.group(1)} to {per_m.group(2)}"

        for page in doc:
            pt = page.get_text()
            page_texts.append(pt)
            full_text += "\n" + pt

        sum_m = re.search(r'STATEMENT\s*SUMMARY[^\n]*\n+Opening\s*Balance[^\n]*\n+([\d,]+\.\d{2})\s+(\d+)\s+(\d+)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})', full_text, re.IGNORECASE)
        if not sum_m:
            sum_m = re.search(r'([\d,]+\.\d{2})\s+(\d+)\s+(\d+)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})', full_text)

        if sum_m:
            metadata["opening_balance"] = clean_amount(sum_m.group(1))
            metadata["total_withdrawals"] = clean_amount(sum_m.group(4))
            metadata["total_deposits"] = clean_amount(sum_m.group(5))
            metadata["closing_balance"] = clean_amount(sum_m.group(6))
        else:
            ob_m = re.search(r'Opening\s*Balance[\s\S]{0,200}?([\d,]+\.\d{2})', full_text, re.IGNORECASE)
            if ob_m:
                metadata["opening_balance"] = clean_amount(ob_m.group(1))

        doc.close()
    except Exception as e:
        print(f"HDFC Metadata error: {e}")

    records = []
    try:
        if isinstance(pdf_source, bytes):
            coord_doc = pymupdf.open(stream=pdf_source, filetype="pdf")
        else:
            coord_doc = pymupdf.open(pdf_source)
        if coord_doc.is_encrypted and password:
            coord_doc.authenticate(password)

        txns_raw = []
        curr_t = None
        date_pattern = re.compile(r'^\d{2}/\d{2}/\d{2,4}$')
        boilerplate_noise = {"HOLDERS", "RTGS/NEFT", "Generated", "Opening", "Cust", "SAVINGS", "Branch", "Statement", "JAKHAN", "SHASTRADHARA"}

        for p_idx in range(len(coord_doc)):
            p_words = coord_doc[p_idx].get_text('words')
            min_y = 220 if p_idx == 0 else 140
            tbl_words = [w for w in p_words if w[1] >= min_y and w[1] <= 780]

            for w in sorted(tbl_words, key=lambda x: (round(x[1] / 3.2) * 3.2, x[0])):
                if w[0] < 60 and date_pattern.match(w[4]):
                    if curr_t:
                        txns_raw.append(curr_t)
                    curr_t = {'date': w[4], 'narr': [], 'bals': []}
                elif curr_t:
                    if any(noise in w[4] for noise in boilerplate_noise) or 'STATEMENT' in w[4] or 'SUMMARY' in w[4] or 'Page' in w[4]:
                        continue
                    x = w[0]
                    if x > 560:
                        curr_t['bals'].append(w[4])
                    elif 55 <= x < 350:
                        curr_t['narr'].append(w[4])

        if curr_t:
            txns_raw.append(curr_t)

        coord_doc.close()

        if txns_raw:
            prev_balance = metadata["opening_balance"]
            for t in txns_raw:
                bal_tokens = [w for w in t['bals'] if re.match(r'^-?[\d,]+\.\d{2}$', w)]
                balance = clean_amount(bal_tokens[-1]) if bal_tokens else prev_balance
                if not records and metadata["opening_balance"] == 0.0:
                    prev_balance = balance
                    metadata["opening_balance"] = balance

                diff = round(balance - prev_balance, 2)
                if diff > 0:
                    credit = diff
                    debit = 0.0
                else:
                    debit = abs(diff)
                    credit = 0.0
                prev_balance = balance

                raw_narr = " ".join(t['narr']).strip()
                cleaned_narr = re.sub(r'(?:BANK\s+LIMITED|Cust\s+\d+|Account\s+A/C|HOLDERS\s*:|RTGS/NEFT|Statement\s*:|Opening\s+Balance|Dr\s+Count\s+[\d,.]+).*$', '', raw_narr, flags=re.I).strip()
                narration = cleaned_narr if len(cleaned_narr) >= 3 else raw_narr
                category = categorize_transaction(narration, debit, credit)

                records.append({
                    "bank": "HDFC Bank",
                    "account_no": metadata["account_no"],
                    "txn_date": t['date'],
                    "iso_date": parse_iso_date(t['date']),
                    "narration": narration,
                    "debit": debit,
                    "credit": credit,
                    "balance": balance,
                    "category": category
                })
    except Exception as e:
        print(f"HDFC Coordinate Extractor fallback: {e}")

    if not records:
        _pg_date = re.compile(r'^\d{2}/\d{2}/\d{2,4}\b')

        def _hdfc_page_lines(pt):
            pl = pt.split('\n')
            for idx, ln in enumerate(pl):
                if _pg_date.match(ln.strip()):
                    return pl[idx:]
            return pl

        if _raw_lines:
            lines = full_text.split('\n')
        else:
            lines = []
            for pt in page_texts:
                lines.extend(_hdfc_page_lines(pt))
            if not lines:
                lines = full_text.split('\n')
        date_pat = re.compile(r'^\d{2}/\d{2}/\d{2,4}\b')
        amt_pat = re.compile(r'^-?[\d,]+\.\d{2}$')

        ignore_headers = (
            "HDFC BANK", "We understand", "JOINT HOLDERS", "Nomination", "From:", "Date Narration",
            "Page No", "Account Branch", "Address", "City", "State", "Phone no", "OD Limit",
            "Currency", "Email", "Cust ID", "Account No", "A/C Open Date", "Account Status",
            "RTGS/NEFT", "MICR", "Branch Code", "Account Type", "Statement of account",
            "*Closing balance", "Contents of this", "State account", "HDFC Bank GSTIN",
            "Registered Office", "STATEMENT SUMMARY", "Generated On", "Generated By",
            "Requesting Branch", "This is a computer", "Opening Balance Dr Count", "Dr Count Cr Count"
        )

        txn_blocks = []
        curr_block = []
        for line in lines:
            l_str = line.strip()
            if not l_str:
                continue
            if "STATEMENT SUMMARY" in l_str:
                if curr_block:
                    txn_blocks.append(curr_block)
                    curr_block = []
                break
            if any(l_str.startswith(h) for h in ignore_headers):
                continue
            if date_pat.match(l_str):
                if curr_block:
                    txn_blocks.append(curr_block)
                curr_block = [l_str]
            elif curr_block:
                curr_block.append(l_str)

        if curr_block:
            txn_blocks.append(curr_block)

        prev_balance = metadata["opening_balance"]

        for block in txn_blocks:
            full_block_text = " ".join(block)
            tokens = full_block_text.split()
            if not tokens or not date_pat.match(tokens[0]):
                continue

            txn_date = tokens[0]
            num_indices = [i for i, t in enumerate(tokens) if amt_pat.match(t)]
            if len(num_indices) < 2:
                continue

            balance = clean_amount(tokens[num_indices[-1]])
            amt = clean_amount(tokens[num_indices[-2]])

            part1 = tokens[1:num_indices[-2]]
            part2 = tokens[num_indices[-1] + 1:]

            clean_narr_tokens = []
            for tok in (part1 + part2):
                if date_pat.match(tok) or (len(tok) >= 12 and tok.isdigit()):
                    continue
                clean_narr_tokens.append(tok)

            narration = " ".join(clean_narr_tokens).strip()
            if not narration:
                narration = " ".join(part1 + part2).strip()

            if not records and metadata["opening_balance"] == 0.0:
                prev_balance = _infer_opening_balance(balance, amt, narration)
                metadata["opening_balance"] = prev_balance

            diff = round(balance - prev_balance, 2)
            if diff > 0:
                credit = diff if abs(diff - amt) > 0.01 else amt
                debit = 0.0
            else:
                debit = abs(diff) if abs(abs(diff) - amt) > 0.01 else amt
                credit = 0.0
            prev_balance = balance
            records.append({
                "bank": "HDFC Bank",
                "account_no": metadata["account_no"],
                "txn_date": txn_date,
                "iso_date": parse_iso_date(txn_date),
                "narration": narration,
                "debit": debit,
                "credit": credit,
                "balance": balance,
                "category": categorize_transaction(narration, debit, credit)
            })

    if not records and not _raw_lines:
        return parse_hdfc_pdf(pdf_source, password=password, _raw_lines=True)
        
    if records:
        if metadata["closing_balance"] == 0.0:
            metadata["closing_balance"] = records[-1]["balance"]
        if metadata["total_withdrawals"] == 0.0:
            metadata["total_withdrawals"] = round(sum(r["debit"] for r in records), 2)
            metadata["total_deposits"] = round(sum(r["credit"] for r in records), 2)
        _fill_period_from_records(metadata, records)

    return records, metadata
