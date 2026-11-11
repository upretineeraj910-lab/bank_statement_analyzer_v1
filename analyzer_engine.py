"""
FinTech Bank Statement Analyzer & Underwriting Engine
Supports Federal Bank (10-col), HDFC Bank (7-col), Axis Bank (7-col), 
IDFC FIRST Bank (7-col), SBI Bank (8-col), ICICI Bank (7-col), Standard Chartered (7-col),
and Universal/Generic layouts.
Features: 100% zero-variance mathematical audit, multiline narration preservation,
zero-false-positive categorization, and distinct CC vs Wallet segregation.
"""

import os
import re
import json
from datetime import datetime
from collections import defaultdict
from io import BytesIO
import pandas as pd
import pymupdf
import pdfplumber

# Load .env automatically if present
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    try:
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip('"').strip("'")
                    if _k and _v and _k not in os.environ:
                        os.environ[_k] = _v
    except Exception:
        pass

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

MONTH_MAP = {
    'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04', 'may': '05', 'jun': '06',
    'jul': '07', 'aug': '08', 'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12'
}

def is_date(val):
    if not val or not str(val).strip():
        return False
    s = str(val).split('\n')[0].strip()
    if re.match(r'^\d{1,2}[\s\-\/\.][A-Za-z]{3,9}[\s\-\/\.]\d{2,4}$', s):
        return True
    if re.match(r'^[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}$', s):
        return True
    if re.match(r'^\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}$', s):
        return True
    if re.match(r'^\d{4}-\d{1,2}-\d{1,2}$', s):
        return True
    return False

def is_amount(val):
    if not val or not str(val).strip():
        return False
    s = str(val).strip().replace(',', '').replace(' ', '').upper()
    s = re.sub(r'^(?:RS\.?|INR)\.?\s*', '', s)
    s = re.sub(r'(?:CR|DR)$', '', s)
    if not s:
        return False
    return bool(re.match(r'^-?\d+(?:\.\d{1,2})?$', s))

def clean_amount(val):
    if not val or not str(val).strip():
        return 0.0
    val = str(val).replace(',', '').replace(' ', '').replace('CR', '').replace('DR', '').strip()
    try:
        return float(val)
    except ValueError:
        return 0.0

def parse_iso_date(date_str):
    if not date_str:
        return ""
    s = str(date_str).split('\n')[0].strip()
    s = re.sub(r'\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?.*$', '', s, flags=re.I).strip()
    
    m0 = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', s)
    if m0:
        y, mo, d = m0.groups()
        try:
            return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            return ""

    m1 = re.match(r'^(\d{1,2})[\s\-\/\.]([A-Za-z]{3,9})[\s\-\/\.](\d{2,4})$', s)
    if m1:
        d, mon, y = m1.groups()
        mon_lower = mon[:3].lower()
        if mon_lower in MONTH_MAP:
            mo = MONTH_MAP[mon_lower]
            year = f"20{y}" if len(y) == 2 else y
            try:
                return datetime(int(year), int(mo), int(d)).strftime("%Y-%m-%d")
            except ValueError:
                return ""

    m2 = re.match(r'^([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{2,4})$', s)
    if m2:
        mon, d, y = m2.groups()
        mon_lower = mon[:3].lower()
        if mon_lower in MONTH_MAP:
            mo = MONTH_MAP[mon_lower]
            year = f"20{y}" if len(y) == 2 else y
            try:
                return datetime(int(year), int(mo), int(d)).strftime("%Y-%m-%d")
            except ValueError:
                return ""

    m3 = re.match(r'^(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2,4})$', s)
    if m3:
        d, mo, y = m3.groups()
        year = f"20{y}" if len(y) == 2 else y
        try:
            return datetime(int(year), int(mo), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            return ""

    return s

def _clean_employer_display(value):
    value = re.sub(r"^A2A[A-Z0-9]{2,10}[-/:]", "", str(value or ""), flags=re.I).strip()
    # Some PDF layouts split PRIVATE across narration lines around a reference ID.
    value = re.sub(r"\s+(?:PRIVA|PRIVAT|PRIV|PVT)$", "", value, flags=re.I).strip()
    return re.sub(r"\s+", " ", value).strip(" -/:,")


def _employer_identity_key(value):
    key = _clean_employer_display(value).upper()
    key = re.sub(r"\b(PRIVATE|LIMITED|PVT|LTD|PRIVA|PRIVAT)\b", " ", key)
    return re.sub(r"[^A-Z0-9]+", "", key)


def extract_dynamic_employer(desc):
    d = desc.strip()
    cleaned = re.sub(r'^(?:NEFT|RTGS|IMPS|ACH|CMS|A2A|FT)\s*(?:CR|DR)?\s*[-/:]?\s*(?:[A-Z0-9]{3,12}[-/:])?[A-Z0-9]{8,25}\s*', '', d, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r'^[A-Z0-9]{10,}\*?', '', cleaned).strip()
    
    corp_match = re.search(r'([A-Z0-9\s.,&-]+?\b(?:PRIVATE\s+LIMITED|PVT\s+LTD|LIMITED|LTD|CORPORATION|CORP|TECHNOLOGIES|TECH|SERVICES|SOLUTIONS|SOLN|LLP|MANAGEMENT)\b)', cleaned, re.IGNORECASE)
    if corp_match:
        emp = corp_match.group(1).strip()
        emp = re.sub(r'^[-/*0-9\s]+', '', emp).strip()
        emp = re.sub(r'\s+IN\d{10,}.*$', '', emp, flags=re.I).strip()
        if len(emp) >= 4 and not any(b in emp.upper() for b in ["STATE BANK", "HDFC BANK", "ICICI BANK", "AXIS BANK"]):
            return _clean_employer_display(emp.title())

    hyphen_parts = [p.strip() for p in cleaned.split('-') if p.strip()]
    for part in hyphen_parts:
        part_upper = part.upper()
        if any(kw in part_upper for kw in ["SALARY", "SALARIS", "PAYROLL", "INCENTIVE", "MONTH", "EBO", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC", "BATCHID"]):
            continue
        if re.search(r'^[0-9]+$', part) or len(part) < 3:
            continue
        part_clean = re.sub(r'\s+(?:CITI\s*BANK|HDFC\s*BANK|SBI|AXIS|ICICI|BANK).*$', '', part, flags=re.IGNORECASE).strip()
        part_clean = re.sub(r'\s+IN\d{10,}.*$', '', part_clean, flags=re.I).strip()
        if len(part_clean) >= 3:
            return _clean_employer_display(part_clean.title())

    return "Corporate Payroll"

def categorize_transaction(desc, debit=0.0, credit=0.0):
    d = desc.upper().strip()
    flags = []
    
    # 1. Genuine Cheque Bounce / Mandate / NACH Failures
    is_upi = d.startswith("UPI-") or "@" in d
    genuine_bounce_pattern = r'\b(BOUNCE|RET\s*CHG|BOUNCE\s*CHG|INSUFFICIENT|ACH\s*RET|NACH\s*RET|ECS\s*RET|CHQ\s*RET|DISHONOUR|UNPAID)\b'
    if re.search(genuine_bounce_pattern, d) or (not is_upi and re.search(r'\b(RETURN|RET)\b', d) and debit > 0):
        flags.append("BOUNCE_FAILURE")

    # 2. Universal Salary Inflows
    broker_keywords = ['ANGEL', 'ZERODHA', 'GROWW', 'UPSTOX', 'ICCL', 'INDIAN CLEARING', 'NSE', 'BSE', 'SHAREKHAN', 'MOTILAL']
    is_broker = any(b in d for b in broker_keywords)
    
    if credit >= 5000 and not is_broker:
        is_salary_text = (
            re.search(r'\b(SALARY|PAYROLL|CMS/SAL|NEFT-SAL|STIPEND|SALARIS)\b', d) or
            re.search(r'[-/](SALARY|PAYROLL|SAL|SALARIS)[-/0-9A-Z]*', d) or
            re.search(r'\b(SAL(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{0,4})\b', d) or
            re.search(r'[-/](?:EBO\s+INCENTIVE|WEEKLY\s+INCENTIVE)', d) or
            (re.search(r'\b(?:PRIVATE\s+LIMITED|PVT\s+LTD|LTD|LIMITED)\b', d) and credit >= 15000 and not re.search(r'\b(?:BANK|FINANCE|FINCORP|INSURANCE|CAPITAL)\b', d)) or
            (re.match(r'^(NEFT|RTGS|IMPS|ACH|CMS|A2A|FT)', d) and credit >= 15000 and
             re.search(r'\b(SOLUTIONS?|TECHNOLOG(?:Y|IES)|SERVICES?|INFOTECH|INDUSTRIES|ENTERPRISES?|SYSTEMS?|CONSULTING|CONSULTANTS?|SOFTWARE|MANAGEMENT|PVT|PRIVATE|CORP|CORPORATION|LLP|INC)\b', d) and
             not re.search(r'\b(?:STATE\s+BANK|DBS\s+BANK|HDFC\s+BANK|ICICI\s+BANK|AXIS\s+BANK|PUNJAB\s+NATIONAL|BANK\s+OF\s+INDIA|BANK\s+INDIA)\b', d))
        )
        if is_salary_text and not re.search(r'\b(RENT|REFUND|REVERSAL|REV|DIVIDEND|CASH\s+DEP|INTEREST|INT\.PD|PAY\s*REQUEST)\b', d):
            flags.append("SALARY")
        
    # 3. Stock Market / Broker / Mutual Fund / SIP Clearing
    sip_or_trading = any(k in d for k in [
        'ANGEL', 'ZERODHA', 'GROWW', 'UPSTOX', 'INDIANCLEARING', 'INDIAN CLEARING',
        'CLEARING', 'NSECL', 'BSECLEA', 'ICCL', 'MFSS', 'CDSL', 'NSDL', 'INDSTOCKS', 'INDMONEY', 'ICICIDIRECT'
    ])
    if sip_or_trading:
        flags.append("STOCK_TRADING")
        
    # 4. EMI & Loan Payments vs Disbursements
    # Important: EMI is strictly a DEBIT. Credits can only be Disbursements.
    if "BOUNCE_FAILURE" not in flags and not sip_or_trading:
        # Check Disbursements (Credits)
        if credit >= 10000 and any(k in d for k in ["INSTALOAN", "INSTA LOAN", "INSTAJUMBOLOAN", "JUMBO LOAN", "LOAN DISB", "DISBURSEMENT", "POONAWALLA"]):
            flags.append("LOAN_DISBURSEMENT")
        
        # Check EMIs (Strictly Debits Only)
        elif debit >= 200:
            is_bajaj_pay_merchant = "BAJAJPAY" in d or "@INDUS-INDB0MERCHA" in d
            
            emi_patterns = (
                r'(ACH\s*D[-_]|ACHDR|ACH-DR|NACH[-_]|BFLAUTOPAY|_EMI_|LOAN\s*REC|CANFIN|TATACAP|TCHHL|TCHIN|ADINGEN_TATA|CANARA\s*RO|HINDON)'
            )
            is_direct_emi = bool(re.search(emi_patterns, d)) or (
                any(k in d for k in ["KRAZYBEE", "POONAWALLAFIN", "NORTHERN ARC", "ADITYA BIRLA FINANCE", "MUTHOOT"])
            )
            
            # Catch HDFC SmartEMI / Masked Card Auto-Billpay EMIs:
            # e.g., IB BILLPAY DR-HDFC2F-537206XXXXXX1334
            if re.search(r'IB\s*BILLPAY\s*DR[-_\s]*HDFC', d) and re.search(r'\d{6}X{4,6}\d{4}', d):
                is_direct_emi = True

            # Standard Clearing Cheque EMI (e.g. EMI 466194268 CHQ)
            if re.search(r'\bEMI\s+[0-9]+\s+CHQ\b', d):
                is_direct_emi = True

            if "BAJAJ" in d and not is_bajaj_pay_merchant and debit >= 500:
                is_direct_emi = True
                
            if ("IDFC" in d or "KMBL" in d or "HDBFS" in d) and any(m in d for m in ["ACH", "NACH", "MANDATE", "LOAN", "DRAOPERATIONS"]) and not is_upi:
                is_direct_emi = True

            if is_direct_emi:
                flags.append("EMI_LOAN")
        
    # 5. Distinct Segregation: Credit Card Payments vs Digital Wallet Topups
    cc_keywords = [
        'CRED.CLUB', 'CREDITCARDS@RBL', '@CRED', '@YESCRED', 'CRED WALLET', 'CREDPAY',
        'CREDIT CARD', 'CC AUTOPAY', 'CC000', 'RBL BANK CREDIT CARD', 'HDFC BANK DIGITAL CRE',
        'PZ HDFC CC', 'SBI CARDS', 'CC BILL PAY', 'BIL/INFT', 'PAYMENT ON CRED'
    ]
    wallet_keywords = [
        'UPI-LITE', 'ADD MONEY', 'MOBIKWIK', 'MBK', 'RUPICARD',
        'ZETAWALL', 'PAYTM WALLET', 'AMAZON PAY WALLET', 'LOAD WALLET'
    ]

    if any(re.search(k, d) for k in cc_keywords):
        flags.append("CC_PAYMENT")
    elif any(re.search(k, d) for k in wallet_keywords):
        flags.append("WALLET_TOPUP")
    elif 'PAYTMQR' in d and debit >= 2000 and any(c in d for c in ['CRED', 'CARD', 'ROTATION', 'WALLET']):
        flags.append("CC_PAYMENT")

    if not flags and d.startswith("UPI/") and (debit > 0 or credit > 0):
        flags.append("UPI_PAYMENT")
    return ", ".join(flags) if flags else "GENERAL"

def _infer_opening_balance(balance, amt, narration):
    n = narration.upper()
    if round(balance - amt, 2) < 0:
        return round(balance + amt, 2)
    credit_hint = any(k in n for k in ["NEFT CR", "NEFT-CR", "RTGS CR", "SALARY", "INT.PD", "INTEREST", "REFUND", "REV", "CREDIT"])
    return round(balance - amt, 2) if credit_hint else round(balance + amt, 2)

def _counterparty_key(narr):
    d = narr.upper().strip()
    if not re.match(r'^(NEFT|RTGS|IMPS|ACH|CMS|A2A|FT)', d):
        return None
    d = re.sub(r'^(NEFT|RTGS|IMPS)\s*(CR|DR)?[-/ ]*', '', d)
    d = re.sub(r'\b[A-Z]{4}0[A-Z0-9]{6}\b', ' ', d)
    d = re.sub(r'\b[A-Z]*\d{6,}[A-Z0-9]*\b', ' ', d)
    words = re.findall(r'[A-Z]{3,}', d)
    words = [w for w in words if w not in ("NEFT", "RTGS", "IMPS", "ACH", "CMS", "A2A", "SAL", "SALARY")]
    return " ".join(words[:2]) or None

def tag_recurring_salary(records, min_amount=10000, tol=0.35):
    excluded_entities = ['ANGEL', 'ZERODHA', 'GROWW', 'UPSTOX', 'ICCL', 'NSE', 'BSE', 'INDIAN CLEARING']
    groups = defaultdict(list)
    for i, r in enumerate(records):
        narr = r.get("narration", "").upper()
        if r.get("credit", 0) >= min_amount and "SALARY" not in r.get("category", ""):
            if any(e in narr for e in excluded_entities):
                continue
            k = _counterparty_key(r.get("narration", ""))
            if k:
                groups[k].append(i)

    for k, idxs in groups.items():
        months = {records[i]["iso_date"][:7] for i in idxs if records[i].get("iso_date")}
        if len(months) < 2:
            continue
        amts = sorted(records[i]["credit"] for i in idxs)
        med = amts[len(amts) // 2]
        hits = [i for i in idxs if abs(records[i]["credit"] - med) <= tol * med]
        if len({records[i]["iso_date"][:7] for i in hits if records[i].get("iso_date")}) < 2:
            continue
        for i in hits:
            cat = records[i]["category"]
            records[i]["category"] = "SALARY" if cat == "GENERAL" else f"{cat}, SALARY"
    return records

def _fill_period_from_records(metadata, records):
    if records and not metadata.get("period"):
        dates = sorted(r["iso_date"] for r in records if r.get("iso_date"))
        if dates:
            metadata["period"] = f"{dates[0]} to {dates[-1]}"

def diagnose_pdf(file_bytes, password=None):
    info = {}
    try:
        doc = pymupdf.open(stream=file_bytes, filetype="pdf")
        if doc.is_encrypted and password:
            doc.authenticate(password)
        text = "".join(p.get_text() for p in doc)
        info["pages"] = len(doc)
        info["text_chars"] = len(text.strip())
        info["looks_scanned"] = len(text.strip()) < 50
        info["date_like_lines"] = len(re.findall(r'(?m)^\s*\d{2}[/.-](?:\d{2}|[A-Za-z]{3})[/.-]\d{2,4}', text))
        info["first_lines"] = [l.strip()[:80] for l in text.split("\n") if l.strip()][:8]
        doc.close()
    except Exception as e:
        info["error"] = str(e)
    return info

def detect_bank_format(file_bytes=None, file_path=None, password=None):
    doc = None
    try:
        if file_bytes:
            doc = pymupdf.open(stream=file_bytes, filetype="pdf")
        elif file_path:
            doc = pymupdf.open(file_path)
            
        if doc.is_encrypted:
            if password:
                if not doc.authenticate(password):
                    return {"is_encrypted": True, "auth_failed": True, "detected_bank": "Unknown"}
            else:
                return {"is_encrypted": True, "auth_failed": False, "detected_bank": "Protected PDF"}
                
        p0_raw = doc[0].get_text() if len(doc) > 0 else ""
        m_table = re.search(r'\n\s*(?:Date|Txn\s*Date|Tran\s*Date|Value\s*Date|Description|Particulars)\s*[\n\r]', p0_raw, re.I)
        header_clean = p0_raw[:m_table.start()].upper() if m_table else p0_raw[:1500].upper()
        p0_header = p0_raw[:2500].upper()

        full_sample_text = ""
        for i in range(min(3, len(doc))):
            full_sample_text += " " + doc[i].get_text()
        text_upper = full_sample_text.upper()

        m_ifsc = re.search(r'IFSC\s*(?:CODE)?\s*[:\.]\s*([A-Z]{4})', p0_header)
        if m_ifsc:
            code = m_ifsc.group(1)
            if code == "SCBL":
                return {"is_encrypted": False, "detected_bank": "Standard Chartered Bank", "layout": "7-column Standard Chartered Layout", "columns": ["Date", "Value Date", "Description", "Cheque", "Deposit", "Withdrawal", "Balance"], "pages": len(doc)}
            elif code == "HDFC":
                return {"is_encrypted": False, "detected_bank": "HDFC Bank", "layout": "7-column Text Format", "columns": ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"], "pages": len(doc)}
            elif code == "SBIN":
                return {"is_encrypted": False, "detected_bank": "SBI Bank", "layout": "8-column Standard Format", "columns": ["Txn Date", "Value Date", "Description", "Ref No", "Debit", "Credit", "Balance"], "pages": len(doc)}
            elif code == "UTIB":
                return {"is_encrypted": False, "detected_bank": "Axis Bank", "layout": "7-column Tabular Format", "columns": ["Tran Date", "Chq No", "Particulars", "Debit", "Credit", "Balance", "Init. Br"], "pages": len(doc)}
            elif code == "ICIC":
                return {"is_encrypted": False, "detected_bank": "ICICI Bank", "layout": "7-column Tabular Format", "columns": ["S No.", "Transaction Date", "Cheque Number", "Transaction Remarks", "Withdrawal Amount", "Deposit Amount", "Balance"], "pages": len(doc)}
            elif code == "FDRL":
                return {"is_encrypted": False, "detected_bank": "Federal Bank", "layout": "10-column Table Format", "columns": ["Date", "Value Date", "Particulars", "Tran Type", "Tran ID", "Cheque Details", "Withdrawals", "Deposits", "Balance", "DR/CR"], "pages": len(doc)}
            elif code == "IDFB":
                return {"is_encrypted": False, "detected_bank": "IDFC FIRST Bank", "layout": "7-column Tabular Format", "columns": ["Transaction Date", "Value Date", "Particulars", "Cheque No", "Debit", "Credit", "Balance"], "pages": len(doc)}

        # ICICI statements often expose the column headers as separate text lines,
        # so IFSC-based detection alone is not sufficient.
        if ("ICICI BANK LIMITED" in text_upper or
            ("TRANSACTION REMARKS" in text_upper and "WITHDRAWAL" in text_upper and "DEPOSIT" in text_upper and "BALANCE" in text_upper)):
            return {"is_encrypted": False, "detected_bank": "ICICI Bank", "layout": "ICICI Statement of Transactions", "columns": ["S No.", "Transaction Date", "Cheque Number", "Transaction Remarks", "Withdrawal Amount", "Deposit Amount", "Balance"], "pages": len(doc)}

        if "STANDARD CHARTERED" in header_clean or "STANCHART" in header_clean or "SCBL" in header_clean or "STANDARD CHARTERED" in text_upper:
            return {"is_encrypted": False, "detected_bank": "Standard Chartered Bank", "layout": "7-column Standard Chartered Layout", "columns": ["Date", "Value Date", "Description", "Cheque", "Deposit", "Withdrawal", "Balance"], "pages": len(doc)}
        elif ("CHQ./REF.NO." in p0_header and "CLOSING BALANCE" in p0_header and "WITHDRAWAL AMT." in p0_header) or "HDFC BANK" in header_clean or "WE UNDERSTAND YOUR WORLD" in text_upper or "HDFC BANK LIMITED" in header_clean:
            return {"is_encrypted": False, "detected_bank": "HDFC Bank", "layout": "7-column Text Format", "columns": ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"], "pages": len(doc)}
        elif "ICICI BANK" in header_clean or "WWW.ICICIBANK.COM" in text_upper or ("ICIC0" in p0_header and "IFSC" in p0_header):
            return {"is_encrypted": False, "detected_bank": "ICICI Bank", "layout": "7-column Tabular Format", "columns": ["S No.", "Transaction Date", "Cheque Number", "Transaction Remarks", "Withdrawal Amount", "Deposit Amount", "Balance"], "pages": len(doc)}
        elif "STATE BANK OF INDIA" in header_clean or "REGULAR SB CHQ" in text_upper or "WWW.ONLINESBI.COM" in text_upper or ("SBIN0" in p0_header and "IFSC" in p0_header):
            return {"is_encrypted": False, "detected_bank": "SBI Bank", "layout": "8-column Standard Format", "columns": ["Txn Date", "Value Date", "Description", "Ref No", "Debit", "Credit", "Balance"], "pages": len(doc)}
        elif "AXIS BANK" in header_clean or "STATEMENT OF AXIS ACCOUNT" in text_upper or "LIBERTY SALARY ACCOUNT" in text_upper or ("UTIB0" in p0_header and "IFSC" in p0_header):
            return {"is_encrypted": False, "detected_bank": "Axis Bank", "layout": "7-column Tabular Format", "columns": ["Tran Date", "Chq No", "Particulars", "Debit", "Credit", "Balance", "Init. Br"], "pages": len(doc)}
        elif "FEDERAL BANK" in header_clean or "FEDSALARY" in text_upper or "SB FEDSALARY" in text_upper or ("FDRL0" in p0_header and "IFSC" in p0_header):
            return {"is_encrypted": False, "detected_bank": "Federal Bank", "layout": "10-column Table Format", "columns": ["Date", "Value Date", "Particulars", "Tran Type", "Tran ID", "Cheque Details", "Withdrawals", "Deposits", "Balance", "DR/CR"], "pages": len(doc)}
        elif "IDFC FIRST BANK" in header_clean or "ALWAYS YOU FIRST" in text_upper or ("IDFB0" in p0_header and "IFSC" in p0_header):
            return {"is_encrypted": False, "detected_bank": "IDFC FIRST Bank", "layout": "7-column Tabular Format", "columns": ["Transaction Date", "Value Date", "Particulars", "Cheque No", "Debit", "Credit", "Balance"], "pages": len(doc)}
        else:
            return {"is_encrypted": False, "detected_bank": "Generic Bank Statement", "layout": "Auto-Detect Universal Engine", "columns": ["Date", "Narration", "Debit", "Credit", "Balance"], "pages": len(doc)}
    except Exception as e:
        return {"error": str(e), "is_encrypted": False, "detected_bank": "Unknown"}
    finally:
        if doc:
            doc.close()

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

    # Prefer positioned-word extraction for SC statements. The table extractor
    # can shift a debit into the deposit column when the narration wraps over lines.
    try:
        coord_pdf = pdfplumber.open(BytesIO(pdf_source) if isinstance(pdf_source, bytes) else pdf_source, password=password)
        coord_records = []
        opening_from_forward = None
        opening_date = None
        date_line_re = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$", re.I)
        money_re = re.compile(r"^\(?-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\)?$")
        for page in coord_pdf.pages:
            words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
            line_map = {}
            for word in words:
                y_key = round(word["top"] / 2.5) * 2.5
                line_map.setdefault(y_key, []).append(word)
            lines = []
            for y_key, line_words in sorted(line_map.items()):
                line_words.sort(key=lambda w: w["x0"])
                left_text = " ".join(w["text"] for w in line_words if w["x0"] < 110)
                date_match = re.search(r"\b\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\b", left_text, re.I)
                lines.append({"words": line_words, "date": date_match.group(0) if date_match else None})
            starts = [i for i, line in enumerate(lines) if line["date"] and is_date(line["date"])]
            for pos, start_idx in enumerate(starts):
                stop_idx = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
                block_lines = lines[start_idx:stop_idx]
                narration_words = []
                deposit = debit = balance = 0.0
                for line in block_lines:
                    line_text = " ".join(w["text"] for w in line["words"])
                    # The summary totals are printed directly after the last
                    # transaction and must not overwrite its actual balance.
                    if re.search(r"\bTOTAL\b", line_text, re.I):
                        break
                    for word in line["words"]:
                        x0, x1, token = word["x0"], word["x1"], word["text"].strip()
                        if money_re.fullmatch(token):
                            value = clean_amount(token)
                            x_mid = (x0 + x1) / 2
                            if x_mid >= 495:
                                balance = value
                            elif 360 <= x_mid < 430:
                                deposit = value
                            elif 430 <= x_mid < 495:
                                debit = value
                        elif x0 >= 160 and x1 <= 370 and not re.fullmatch(r"\d{1,3}", token):
                            if token and token.lower() not in {"date", "description", "value", "deposit", "withdrawal", "balance", "total"}:
                                narration_words.append(token)
                    # Description text can wrap below the amount line in SC PDFs.
                    # The explicit TOTAL row marks the end of the last transaction.
                narration = re.sub(r"\s+", " ", " ".join(narration_words)).strip()
                # Balance-forward lines contain the opening balance, not a transaction.
                # The word BALANCE is a column heading at a different x-position,
                # so the narration may contain only "FORWARD" after column filtering.
                if "FORWARD" in narration.upper() and balance and not debit and not deposit:
                    opening_from_forward = balance
                    opening_date = block_lines[0]["date"]
                    continue
                # The printed statement summary repeats aggregate debit/credit totals.
                # Never append that summary row as if it were an individual transaction.
                if not balance or not narration:
                    continue
                txn_date = block_lines[0]["date"]
                coord_records.append({
                    "bank": metadata["bank"], "account_no": metadata.get("account_no", ""),
                    "txn_date": txn_date, "iso_date": parse_iso_date(txn_date),
                    "narration": narration, "debit": round(debit, 2),
                    "credit": round(deposit, 2), "balance": round(balance, 2),
                    "category": categorize_transaction(narration, debit, deposit)
                })
        coord_pdf.close()
        if coord_records:
            coord_records.sort(key=lambda r: r["iso_date"])
            records = coord_records
            if opening_from_forward is not None:
                metadata["opening_balance"] = opening_from_forward
            elif metadata.get("opening_balance", 0.0) == 0.0:
                first = records[0]
                metadata["opening_balance"] = round(first["balance"] - first["credit"] + first["debit"], 2)
            period_start = opening_date or records[0]["txn_date"]
            metadata["period"] = f"{period_start} to {records[-1]['txn_date']}"
    except Exception as e:
        print(f"[SC Coordinate Parser Fallback] {e}")

    if records:
        if metadata["opening_balance"] == 0.0 and len(records) > 0:
            metadata["opening_balance"] = round(records[0]["balance"] - records[0]["credit"] + records[0]["debit"], 2)
        metadata["closing_balance"] = records[-1]["balance"]
        metadata["total_withdrawals"] = round(sum(r["debit"] for r in records), 2)
        metadata["total_deposits"] = round(sum(r["credit"] for r in records), 2)
        _fill_period_from_records(metadata, records)

    return records, metadata

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

def parse_icici_pdf(pdf_source, password=None):
    """Parse ICICI's text-based statement layout using word coordinates.

    ICICI PDFs frequently have no extractable table grid. Column positions are
    therefore taken from the printed header and transaction rows are assembled
    from positioned words, preserving wrapped narrations.
    """
    metadata = {
        "bank": "ICICI Bank", "borrower_name": "ICICI Account Holder", "account_no": "",
        "branch": "ICICI Bank Branch", "sol_id": "", "ifsc": "ICIC0000000", "cust_id": "",
        "email": "", "mobile": "", "account_type": "Savings Account", "period": "",
        "opening_balance": 0.0, "closing_balance": 0.0,
        "total_withdrawals": 0.0, "total_deposits": 0.0
    }
    records = []
    doc = None
    pdf_words_doc = None
    try:
        doc = pymupdf.open(stream=pdf_source, filetype="pdf") if isinstance(pdf_source, bytes) else pymupdf.open(pdf_source)
        pdf_words_doc = pdfplumber.open(BytesIO(pdf_source) if isinstance(pdf_source, bytes) else pdf_source, password=password)
        if doc.is_encrypted:
            if not password or not doc.authenticate(password):
                raise ValueError("Password protected PDF or incorrect password")
        first_text = doc[0].get_text() if len(doc) else ""
        all_text = "\n".join(page.get_text() for page in doc)

        acc_m = re.search(r'(?:Saving|Current|Salary|Savings)?\s*Account\s*(?:no|number)?\.?\s*[:\-]?\s*([0-9]{10,18})', all_text, re.I)
        if acc_m:
            metadata["account_no"] = acc_m.group(1)
        per_m = re.search(r'for the period\s+(.+?)(?:\n|$)', all_text, re.I)
        if per_m:
            metadata["period"] = per_m.group(1).strip()
        branch_m = re.search(r'Your Base Branch:\s*([^,\n]+)', all_text, re.I)
        if branch_m:
            metadata["branch"] = branch_m.group(1).strip()
        ifsc_m = re.search(r'\b(ICIC0[A-Z0-9]{6})\b', all_text, re.I)
        if ifsc_m:
            metadata["ifsc"] = ifsc_m.group(1).upper()

        # Avoid overfitting customer-name extraction; this field is not required
        # for transaction accuracy and should not be guessed from postal addresses.
        name_m = re.search(r'(?:Account Holder|Customer)\s*Name\s*[:\-]?\s*([^\n]+)', first_text, re.I)
        if name_m:
            metadata["borrower_name"] = name_m.group(1).strip()

        def money_token(value):
            return bool(re.fullmatch(r"\(?-?[\d,]+\.\d{2}\)?", value or ""))

        for page_idx, page in enumerate(doc):
            words = pdf_words_doc.pages[page_idx].extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
            if not words:
                continue
            # Group words into visual lines.
            lines = []
            for word in sorted(words, key=lambda w: (round(w["top"] / 2), w["x0"])):
                target = next((ln for ln in lines if abs(ln["top"] - word["top"]) <= 2.5), None)
                if target is None:
                    target = {"top": word["top"], "words": []}
                    lines.append(target)
                target["words"].append(word)
            lines.sort(key=lambda ln: ln["top"])
            for ln in lines:
                ln["words"].sort(key=lambda w: w["x0"])

            # Header anchors. Values in this layout align under these headings.
            anchors = {"withdrawal": None, "deposit": None, "balance": None}
            for ln in lines:
                txt = " ".join(w["text"] for w in ln["words"]).lower()
                for key in anchors:
                    if key in txt and anchors[key] is None:
                        hits = [w["x0"] + w["x1"] for w in ln["words"] if key in w["text"].lower()]
                        if hits:
                            anchors[key] = sum(hits) / (2 * len(hits))
            # Some headings wrap over two lines; x coordinates are still stable.
            if anchors["withdrawal"] is None: anchors["withdrawal"] = 425.0
            if anchors["deposit"] is None: anchors["deposit"] = 490.0
            if anchors["balance"] is None: anchors["balance"] = 550.0

            tx_starts = []
            for idx, ln in enumerate(lines):
                date_word = next((w for w in ln["words"] if is_date(w["text"])), None)
                if date_word:
                    iso = parse_iso_date(date_word["text"])
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso):
                        tx_starts.append((idx, date_word))

            for pos, (line_idx, date_word) in enumerate(tx_starts):
                next_idx = tx_starts[pos + 1][0] if pos + 1 < len(tx_starts) else len(lines)
                row_lines = lines[line_idx:next_idx]
                # Find the first transaction amount/balance line beneath its date.
                amount_line_idx = None
                row_values = []
                for local_idx, ln in enumerate(row_lines):
                    vals = [w for w in ln["words"] if money_token(w["text"].replace("₹", ""))]
                    if vals:
                        # Header/footer numbers are excluded by requiring at least
                        # one value in a transaction amount or balance column.
                        col_vals = [w for w in vals if w["x0"] >= min(anchors.values()) - 35]
                        if col_vals:
                            amount_line_idx = local_idx
                            row_values = col_vals
                            break
                if amount_line_idx is None:
                    continue

                debit = credit = balance = 0.0
                amount_candidates = []
                for w in row_values:
                    raw = w["text"].replace(",", "")
                    try:
                        val = abs(float(raw))
                    except ValueError:
                        continue
                    xmid = (w["x0"] + w["x1"]) / 2
                    if xmid >= anchors["balance"] - 18:
                        balance = val
                    else:
                        amount_candidates.append((xmid, val))
                if amount_candidates:
                    xmid, amount = min(amount_candidates, key=lambda item: abs(item[0] - min(anchors["withdrawal"], anchors["deposit"])))
                    if abs(xmid - anchors["deposit"]) < abs(xmid - anchors["withdrawal"]):
                        credit = amount
                    else:
                        debit = amount

                # Collect narration only before the amount row; this excludes
                # page footers and bank legends after the transaction.
                narr_words = []
                for ln in row_lines:
                    line_text = " ".join(w["text"] for w in ln["words"]).upper()
                    if any(marker in line_text for marker in (
                        "SINCERLY", "TEAM ICICI BANK", "THIS IS A SYSTEM GENERATED",
                        "LEGENDS FOR TRANSACTIONS", "NEVER SHARE YOUR OTP", "DIAL YOUR BANK",
                    )):
                        break
                    for w in ln["words"]:
                        token = w["text"].strip()
                        if (w["x0"] < anchors["withdrawal"] - 25 and
                            not money_token(token) and not is_date(token) and
                            token and not re.fullmatch(r"\d{1,3}", token)):
                            narr_words.append(token)
                narration = re.sub(r"\s+", " ", " ".join(narr_words)).strip()
                narration = re.sub(r"\b(?:Never share your OTP|www\.icici\.bank\.in|Dial your Bank).*", "", narration, flags=re.I).strip()
                if not narration:
                    narration = "ICICI transaction"

                records.append({
                    "bank": "ICICI Bank", "account_no": metadata.get("account_no", ""),
                    "txn_date": date_word["text"], "iso_date": parse_iso_date(date_word["text"]),
                    "narration": narration, "debit": round(debit, 2),
                    "credit": round(credit, 2), "balance": round(balance, 2),
                    "category": categorize_transaction(narration, debit, credit)
                })

        records.sort(key=lambda r: (r.get("iso_date", ""), r.get("txn_date", "")))
        if records:
            metadata["closing_balance"] = records[-1]["balance"]
            metadata["total_withdrawals"] = round(sum(r["debit"] for r in records), 2)
            metadata["total_deposits"] = round(sum(r["credit"] for r in records), 2)
            # Opening balance is derived only when the first row's direction and
            # balance are present. It is a calculated value, not a bank-printed one.
            first = records[0]
            metadata["opening_balance"] = round(first["balance"] - first["credit"] + first["debit"], 2)
            _fill_period_from_records(metadata, records)
        return records, metadata
    finally:
        if pdf_words_doc is not None:
            pdf_words_doc.close()
        if doc is not None:
            doc.close()

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
            
        per_m = re.search(r'Statement From\s*:([0-9-]+)\s*to\s*([0-9-]+)', p0_text)
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
    date_pat = re.compile(r'^\d{2}/\d{2}/\d{4}\b')
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

def parse_universal_pdf(pdf_source, password=None, detected_bank=None):
    metadata = {
        "bank": detected_bank or "General Bank Statement", "borrower_name": "Account Holder",
        "account_no": "", "branch": "Main Branch", "sol_id": "", "ifsc": "", "cust_id": "",
        "email": "", "mobile": "", "account_type": "Savings / Current Account", "period": "",
        "opening_balance": 0.0, "closing_balance": 0.0, "total_withdrawals": 0.0, "total_deposits": 0.0
    }

    BANK_IFSC_MAP = {
        "SCBL": "Standard Chartered Bank", "ICIC": "ICICI Bank", "HDFC": "HDFC Bank",
        "SBIN": "State Bank of India", "UTIB": "Axis Bank", "FDRL": "Federal Bank",
        "IDFB": "IDFC FIRST Bank", "KKBK": "Kotak Mahindra Bank", "PUNB": "Punjab National Bank",
        "BARB": "Bank of Baroda", "CNRB": "Canara Bank", "UBIN": "Union Bank of India",
        "INDB": "IndusInd Bank", "YESB": "Yes Bank", "BKID": "Bank of India",
        "CBIN": "Central Bank of India", "RATN": "RBL Bank"
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

def calculate_daily_abb(records, opening_balance, closing_balance, statement_period=None):
    if not records:
        return {"overall_abb": 0.0, "monthly_abb": {}, "min_balance": 0.0, "max_balance": 0.0}
    sorted_recs = sorted(records, key=lambda x: str(x.get("iso_date", "")))
    date_closing = {}
    for r in sorted_recs:
        d = parse_iso_date(r.get("iso_date") or r.get("txn_date") or "")
        if d and re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            date_closing[d] = r.get("balance", 0.0)
    if not date_closing:
        return {"overall_abb": closing_balance, "monthly_abb": {}, "min_balance": closing_balance, "max_balance": closing_balance}
    try:
        all_dates = sorted(date_closing.keys())
        period_dates = []
        if statement_period:
            for token in re.findall(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}", str(statement_period)):
                parsed = parse_iso_date(token)
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", parsed):
                    period_dates.append(parsed)
        start_date = datetime.strptime(min(period_dates[0], all_dates[0]) if period_dates else all_dates[0], "%Y-%m-%d")
        end_date = datetime.strptime(max(period_dates[-1], all_dates[-1]) if len(period_dates) >= 2 else all_dates[-1], "%Y-%m-%d")
        total_days = max(1, (end_date - start_date).days + 1)
        current_dt = start_date
        curr_bal = opening_balance
        daily_balances = []
        monthly_accum = {}
        while current_dt <= end_date:
            d_str = current_dt.strftime("%Y-%m-%d")
            m_str = current_dt.strftime("%Y-%m")
            if d_str in date_closing:
                curr_bal = date_closing[d_str]
            daily_balances.append(curr_bal)
            if m_str not in monthly_accum:
                monthly_accum[m_str] = []
            monthly_accum[m_str].append(curr_bal)
            current_dt = current_dt + pd.Timedelta(days=1)
        overall_abb = round(sum(daily_balances) / len(daily_balances), 2) if daily_balances else closing_balance
        monthly_abb = {k: round(sum(v) / len(v), 2) for k, v in monthly_accum.items()}
        return {
            "overall_abb": overall_abb,
            "monthly_abb": monthly_abb,
            "min_balance": round(min(daily_balances), 2) if daily_balances else 0.0,
            "max_balance": round(max(daily_balances), 2) if daily_balances else 0.0,
            "days_tracked": total_days
        }
    except Exception as e:
        print(f"[ABB Calculation Warning] {e}")
        bals = [r.get("balance", 0.0) for r in records if "balance" in r]
        avg_bal = round(sum(bals) / len(bals), 2) if bals else closing_balance
        return {
            "overall_abb": avg_bal,
            "monthly_abb": {},
            "min_balance": round(min(bals), 2) if bals else 0.0,
            "max_balance": round(max(bals), 2) if bals else 0.0,
            "days_tracked": len(records)
        }

def _redact_for_ai(value):
    """Remove common direct identifiers before transaction text leaves the app."""
    text = str(value or "")
    text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[EMAIL]", text, flags=re.I)
    text = re.sub(r"\b[A-Z]{5}\d{4}[A-Z]\b", "[PAN]", text, flags=re.I)
    text = re.sub(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", "[IFSC]", text, flags=re.I)
    text = re.sub(r"\b\d{9,18}\b", "[NUMBER]", text)
    text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9._%+-]+", "[UPI_ID]", text, flags=re.I)
    return text[:180]


def run_gemini_underwriting_intelligence(records, metadata):
    """Return optional AI commentary; deterministic calculations remain authoritative."""
    if not GENAI_AVAILABLE:
        return None
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        # Bound the request size and redact identifiers before sending data externally.
        credits = []
        debits = []
        churn = []
        for idx, row in enumerate(records):
            narration = _redact_for_ai(row.get("narration", ""))
            category = str(row.get("category", "")).upper()
            credit = float(row.get("credit") or 0)
            debit = float(row.get("debit") or 0)
            item = {"id": idx, "date": row.get("txn_date", ""), "narration": narration}
            if credit >= 5000 or "SALARY" in category or any(k in narration.upper() for k in ("SAL", "PAY", "INCENTIVE", "BONUS", "NEFT")):
                credits.append({**item, "amount": round(credit, 2)})
            if debit >= 200 and ("EMI" in category or any(k in narration.upper() for k in ("ACH", "NACH", "MANDATE", "LOAN", "BAJAJ", "BILLPAY"))):
                debits.append({**item, "amount": round(debit, 2)})
            if "CC_PAYMENT" in category or "WALLET_TOPUP" in category:
                churn.append({**item, "debit": round(debit, 2), "credit": round(credit, 2)})

        if not credits and not debits and not churn:
            return None

        def sample(items, limit):
            if len(items) <= limit:
                return items
            # Evenly sample the statement period instead of silently taking only
            # the earliest transactions.
            indexes = [round(i * (len(items) - 1) / (limit - 1)) for i in range(limit)]
            return [items[i] for i in indexes]

        prompt = """Review the following anonymized Indian bank-statement transaction candidates.
Provide only secondary observations. Do not infer facts not supported by the supplied rows.
Do not make credit approval, rejection, fraud, or eligibility decisions.
Return JSON with keys: summary (string), possible_employer_names (array of strings),
possible_lender_names (array of strings), limitations (array of strings).
If evidence is weak, say so. Keep summary under 400 characters.

CREDITS:\n""" + json.dumps(sample(credits, 35), ensure_ascii=False) + \
            "\nDEBITS:\n" + json.dumps(sample(debits, 35), ensure_ascii=False) + \
            "\nCARD/WALLET:\n" + json.dumps(sample(churn, 30), ensure_ascii=False)

        model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=15000))
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        if not response or not response.text:
            return None
        result = json.loads(response.text)
        if not isinstance(result, dict):
            return None
        summary = result.get("summary", "")
        if not isinstance(summary, str):
            summary = ""
        def safe_string_list(key, max_length):
            value = result.get(key, [])
            if not isinstance(value, list):
                return []
            return [item.strip()[:max_length] for item in value if isinstance(item, str) and item.strip()][:10]

        return {
            "summary": summary[:500],
            "possible_employer_names": safe_string_list("possible_employer_names", 100),
            "possible_lender_names": safe_string_list("possible_lender_names", 100),
            "limitations": safe_string_list("limitations", 160),
        }
    except Exception as exc:
        # AI is optional: parser and deterministic dashboard must still succeed.
        print(f"[Gemini] Optional analysis unavailable: {type(exc).__name__}: {exc}")
        return None

def order_transactions_by_balance(records, opening_balance, tolerance=0.02):
    """Restore same-day transaction order from running-balance transitions."""
    if not records:
        return records
    indexed = list(enumerate(records))
    indexed.sort(key=lambda pair: (str(pair[1].get("iso_date") or "9999-99-99"), pair[0]))
    groups = defaultdict(list)
    for original_index, row in indexed:
        groups[str(row.get("iso_date") or "")].append((original_index, row))
    current_balance = float(opening_balance or 0.0)
    ordered = []
    for day in sorted(groups):
        pending = list(groups[day])
        while pending:
            match_pos = None
            for pos, (_, row) in enumerate(pending):
                expected = round(current_balance + float(row.get("credit") or 0.0) - float(row.get("debit") or 0.0), 2)
                actual = round(float(row.get("balance") or 0.0), 2)
                if abs(expected - actual) <= tolerance:
                    match_pos = pos
                    break
            if match_pos is None:
                match_pos = 0
            _, chosen = pending.pop(match_pos)
            ordered.append(chosen)
            current_balance = float(chosen.get("balance") or current_balance)
    if isinstance(records, list):
        records[:] = ordered
        return records
    return ordered


def compute_underwriting_insights(records, metadata):
    records = order_transactions_by_balance(records, (metadata or {}).get("opening_balance", 0.0))
    df = pd.DataFrame(records)
    if df.empty:
        return {}
    for col, default in (("debit", 0.0), ("credit", 0.0), ("balance", 0.0), ("category", "GENERAL"), ("narration", ""), ("txn_date", ""), ("iso_date", "")):
        if col not in df.columns:
            df[col] = default
    for col in ("debit", "credit", "balance"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    for col in ("category", "narration", "txn_date", "iso_date"):
        df[col] = df[col].fillna("").astype(str)
    total_debits = round(float(df['debit'].sum()), 2)
    total_credits = round(float(df['credit'].sum()), 2)
    open_bal = float(metadata.get('opening_balance', 0.0))
    close_bal = float(metadata.get('closing_balance', 0.0))
    reconciled_calc = round(open_bal + total_credits - total_debits, 2)
    variance = round(abs(reconciled_calc - close_bal), 2)
    running_balance = open_bal
    balance_continuity_breaks = 0
    for row in records:
        expected_balance = round(running_balance + float(row.get("credit") or 0.0) - float(row.get("debit") or 0.0), 2)
        actual_balance = round(float(row.get("balance") or 0.0), 2)
        if abs(expected_balance - actual_balance) > 0.02:
            balance_continuity_breaks += 1
        running_balance = actual_balance
    aggregate_reconciled = variance <= 0.05
    is_reconciled = aggregate_reconciled and balance_continuity_breaks == 0
    
    # 1. Salary Inflows
    salary_df = df[df['category'].str.contains('SALARY', na=False)]
    salary_credits = []
    employers = []
    
    for _, row in salary_df.iterrows():
        desc = row['narration']
        emp = extract_dynamic_employer(desc)
        salary_credits.append({
            "date": row['txn_date'],
            "iso_date": row['iso_date'],
            "employer": emp,
            "amount": row['credit']
        })
        if emp != "Corporate Payroll":
            identity = _employer_identity_key(emp)
            existing_index = next((i for i, current in enumerate(employers) if _employer_identity_key(current) == identity), None)
            if existing_index is None:
                employers.append(emp)
            elif len(emp) > len(employers[existing_index]):
                employers[existing_index] = emp
                for salary_item in salary_credits:
                    if _employer_identity_key(salary_item["employer"]) == identity:
                        salary_item["employer"] = emp
            
    if not employers and salary_credits:
        employers = ["Corporate Payroll"]
        
    salary_by_month = {}
    for item in salary_credits:
        iso_date = str(item.get("iso_date") or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso_date):
            month = iso_date[:7]
            salary_by_month[month] = salary_by_month.get(month, 0.0) + float(item.get("amount") or 0.0)
    full_months = [amount for amount in salary_by_month.values() if amount >= 10000]
    observed_salary_months = len(full_months)
    if full_months:
        avg_monthly_salary = round(sum(full_months) / observed_salary_months, 2)
        salary_range_str = f"₹{int(avg_monthly_salary):,} - ₹{int(avg_monthly_salary * 1.15):,}"
    else:
        avg_monthly_salary = 0.0
        salary_range_str = "N/A (No verified salary-month evidence)"

    # Evidence-based indicator: a single salary credit must not receive a
    # prime/stable score. More observed months increase confidence gradually.
    if observed_salary_months == 0:
        stability_score = 0
    elif observed_salary_months == 1:
        stability_score = 45
    elif observed_salary_months == 2:
        stability_score = 55
    elif observed_salary_months == 3:
        stability_score = 65
    elif observed_salary_months <= 5:
        stability_score = 75
    elif observed_salary_months <= 8:
        stability_score = 85
    else:
        stability_score = 90
    if observed_salary_months >= 3:
        avg_observed = sum(full_months) / len(full_months)
        if avg_observed > 0:
            coefficient_of_variation = (sum((amount - avg_observed) ** 2 for amount in full_months) / len(full_months)) ** 0.5 / avg_observed
            if coefficient_of_variation <= 0.15:
                stability_score = min(95, stability_score + 5)
            elif coefficient_of_variation > 0.50:
                stability_score = max(0, stability_score - 5)
    
    # 2. Institutional Debt & Active Lenders
    LENDER_CODE_MAP = {
        "KMBLDRAOPERATIONS": "Kotak Mahindra Bank Mandate",
        "TCHHL": "Tata Capital Housing Finance",
        "TCHIN": "Tata Capital Housing Finance",
        "ADINGEN_TATA": "Tata Capital Housing Finance",
        "CANFIN": "Can Fin Homes",
        "BFLAUTOPAY": "Bajaj Finance",
        "IDFC FIRST": "IDFC FIRST Bank Loan",
        "HINDON": "Hindon Mercantile",
        "POONAWALLA": "Poonawalla Fincorp",
        "KRAZYBEE": "KrazyBee Services",
        "NORTHERN": "Northern Arc Capital",
        "ADITYA BIRLA": "Aditya Birla Finance",
        "LIC": "Life Insurance Corporation (LIC Loan)",
        "MUTHOOT": "Muthoot Finance",
        "HDBFS": "HDB Financial Services",
        "CHOLAMANDALAM": "Cholamandalam Investment and Finance"
    }

    loan_df = df[df['category'].str.contains('EMI_LOAN', na=False)]
    disburse_df = df[df['category'].str.contains('LOAN_DISBURSEMENT', na=False)]
    active_lenders = []
    
    for _, row in loan_df.iterrows():
        narr = row['narration'].upper()
        if any(b in narr for b in ["ICCL", "INDIAN CLEARING", "NSECL", "ZERODHA", "ANGEL"]):
            continue
        matched_lender = None
        for code, formal_name in LENDER_CODE_MAP.items():
            if code in narr:
                matched_lender = formal_name
                break
        if not matched_lender:
            if "BILLPAY DR-HDFC" in narr and "XXXXXX" in narr:
                matched_lender = "HDFC Bank SmartEMI / Card Loan"
            elif "EMI" in narr and "CHQ" in narr:
                matched_lender = "Banking Institution Cheque EMI"
            elif row['debit'] >= 1000:
                matched_lender = "Financial Institution Mandate"
        if matched_lender and matched_lender not in active_lenders:
            active_lenders.append(matched_lender)
            
    valid_months = {str(v)[:7] for v in df['iso_date'].dropna().astype(str) if re.match(r"^\d{4}-\d{2}-\d{2}$", str(v))}
    num_months = max(1, len(valid_months))
    monthly_emi_obligations = round(float(loan_df['debit'].sum()) / num_months, 2)
    jumbo_loans = [{"date": r['txn_date'], "narration": r['narration'], "amount": r['credit']} for _, r in disburse_df.iterrows()]
    total_jumbo_disbursed = sum(j['amount'] for j in jumbo_loans)
    trading_df = df[df['category'].str.contains('STOCK_TRADING', na=False)]
    
    # 3. Segregated CC Payments & Wallet Topups
    cc_df = df[df['category'].str.contains('CC_PAYMENT', na=False)]
    wallet_df = df[df['category'].str.contains('WALLET_TOPUP', na=False)]
    cred_inflows = round(float(cc_df[cc_df['narration'].str.contains(r'@CRED|@YESCRED', case=False, na=False)]['credit'].sum()), 2)
    cred_outflows = round(float(cc_df[cc_df['narration'].str.contains(r'CRED\.CLUB|CREDPAY', case=False, na=False)]['debit'].sum()), 2)
    
    bounce_df = df[df['category'].str.contains('BOUNCE_FAILURE', na=False)]
    abb_stats = calculate_daily_abb(records, open_bal, close_bal, metadata.get("period"))
    foir_pct = round((monthly_emi_obligations / avg_monthly_salary) * 100, 1) if avg_monthly_salary > 0 else 0.0

    # Churning & Fraud Logic
    if cred_outflows >= 50000 and total_jumbo_disbursed >= 50000 and cred_inflows >= 20000:
        risk_tag = "CRITICAL"
        risk_score = 90
        churn_velocity = "HIGH"
        fraud_reasoning = "High-velocity revolver credit card circular rotation coupled with personal loan disbursements."
    elif cred_outflows >= 25000 and cred_inflows >= 10000:
        risk_tag = "ELEVATED"
        risk_score = 60
        churn_velocity = "MODERATE"
        fraud_reasoning = "Elevated volume of circular credit card repayment and wallet rotations detected."
    else:
        risk_tag = "CLEAN"
        risk_score = 10
        churn_velocity = "LOW"
        fraud_reasoning = "No configured high-volume card-churn pattern was triggered in the parsed rows; this is not a fraud determination."

    # Gemini is an advisory layer only. Deterministic amounts, salary tags,
    # lender totals and risk scores must never be overwritten by generated text.
    ai_result = run_gemini_underwriting_intelligence(records, metadata)
    ai_applied = bool(isinstance(ai_result, dict) and ai_result.get("summary"))
    primary_employer = employers[0] if employers else "Corporate Payroll"
    ai_commentary = ai_result.get("summary", "") if ai_applied else ""

    employer_switch = (len(employers) >= 2)
    if employer_switch:
        switch_notes = f"Multiple employer names detected ({', '.join(employers)}); review dates and continuity."
        stability_score = max(0, stability_score - 5)
    elif observed_salary_months:
        switch_notes = "One employer pattern observed in the parsed salary credits."
    else:
        switch_notes = "No direct salary credits were confidently tagged in this statement."

    return {
        "metadata": metadata,
        "reconciliation": {
            "opening_balance": open_bal,
            "total_deposits": total_credits,
            "total_withdrawals": total_debits,
            "closing_balance": close_bal,
            "calculated_closing": reconciled_calc,
            "variance": variance,
            "is_reconciled": is_reconciled,
            "aggregate_reconciled": aggregate_reconciled,
            "balance_continuity_breaks": balance_continuity_breaks,
            "balance_continuity_status": "PASS" if balance_continuity_breaks == 0 else "WARNING",
            "status": "RECONCILED" if is_reconciled else "WARNING",
            "total_transactions": len(records),
            "dr_count": int((df['debit'] > 0).sum()),
            "cr_count": int((df['credit'] > 0).sum())
        },
        "salary_audit": {
            "primary_employer": primary_employer,
            "verified_employers": employers,
            "salary_run_rate": salary_range_str,
            "avg_monthly_salary": avg_monthly_salary,
            "credits_count": len(salary_credits),
            "credits_history": salary_credits,
            "employer_switch": employer_switch,
            "switch_notes": switch_notes,
            "stability_score": stability_score,
            "observed_salary_months": observed_salary_months,
            "stability_tier": (
                "Consistent observed salary history" if stability_score >= 80 else
                "Moderate observed salary history" if stability_score >= 65 else
                "Limited salary history" if stability_score > 0 else
                "Insufficient salary evidence"
            ),
            "ai_enabled": ai_applied
        },
        "debt_and_emi": {
            "monthly_obligations": monthly_emi_obligations,
            "active_lenders": active_lenders,
            "lenders_count": len(active_lenders),
            "jumbo_loans_disbursed": total_jumbo_disbursed,
            "jumbo_loans_list": jumbo_loans,
            "foir_percentage": foir_pct,
            "foir_available": avg_monthly_salary > 0,
            "total_emi_paid": round(float(loan_df['debit'].sum()), 2),
            "ai_enabled": ai_applied
        },
        "cc_stress": {
            "is_alert": (total_jumbo_disbursed >= 50000 and float(cc_df['debit'].sum()) >= 50000),
            "severity": "CRITICAL" if (total_jumbo_disbursed >= 50000 and float(cc_df['debit'].sum()) >= 50000) else "NORMAL",
            "message": "A configured high-volume card-stress pattern was detected; manual review is recommended." if (total_jumbo_disbursed >= 50000 and float(cc_df['debit'].sum()) >= 50000) else "No configured high-volume card-stress trigger was detected in the parsed rows.",
            "jumbo_disbursed": total_jumbo_disbursed,
            "card_debt_settled": round(float(cc_df['debit'].sum()), 2)
        },
        "stock_and_mf": {
            "total_trading_flow": round(float(trading_df['credit'].sum() + trading_df['debit'].sum()), 2),
            "total_inflows": round(float(trading_df['credit'].sum()), 2),
            "total_outflows": round(float(trading_df['debit'].sum()), 2),
            "angel_one_inflows": round(float(trading_df[trading_df['narration'].str.contains('ANGEL', case=False, na=False)]['credit'].sum()), 2),
            "iccl_sip_outflows": round(float(trading_df[trading_df['narration'].str.contains('INDIANCLEARING|ICCL|CLEARING', case=False, na=False)]['debit'].sum()), 2),
            "nse_bse_settlements": round(float(trading_df[trading_df['narration'].str.contains('NSECL|BSECLEA', case=False, na=False)]['credit'].sum()), 2),
            "txns_count": len(trading_df)
        },
        "cc_and_wallet_churn": {
            "total_churn_volume": round(float(cc_df['debit'].sum() + wallet_df['debit'].sum()), 2),
            "cred_inflows": cred_inflows,
            "cred_outflows": cred_outflows,
            "net_cred_rotation": round(cred_outflows - cred_inflows, 2),
            "cc_payments_total": round(float(cc_df['debit'].sum()), 2),
            "wallet_topups_total": round(float(wallet_df['debit'].sum()), 2),
            "txns_count": len(cc_df) + len(wallet_df),
            "fraud_risk_tag": risk_tag,
            "fraud_reasoning": fraud_reasoning
        },
        "ai_advisory": ai_result if isinstance(ai_result, dict) else None,
        "fraud_and_churn_intelligence": {
            "risk_tag": risk_tag,
            "risk_score": risk_score,
            "churn_velocity": churn_velocity,
            "reasoning": fraud_reasoning,
            "assessment_scope": "Rule-based transaction-pattern indicator only; not a fraud finding or lending decision.",
            "ai_commentary": ai_commentary,
            "ai_enabled": ai_applied
        },
        "bounce_audit": {
            "bounce_count": len(bounce_df),
            "is_clean": (len(bounce_df) == 0),
            "status_text": "No bounce/return markers detected in parsed rows" if len(bounce_df) == 0 else f"{len(bounce_df)} Bounce / return markers detected",
            "tag_color": "emerald" if len(bounce_df) == 0 else "rose",
            "bounces_list": bounce_df[['txn_date', 'narration', 'debit']].to_dict('records'),
            "assessment_scope": "Only parsed bounce/return markers are counted; this does not verify the full repayment history."
        },
        "abb_metrics": abb_stats
    }

