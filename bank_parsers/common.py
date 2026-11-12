"""Common helper utilities for bank statement parsers.

Includes date parsing, amount cleaning/validation, transaction categorization,
opening balance inference, and period calculation.
"""
from __future__ import annotations

import re
from datetime import datetime

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
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    m1 = re.match(r'^(\d{1,2})[\s\-\/\.]([A-Za-z]{3,9})[\s\-\/\.](\d{2,4})$', s)
    if m1:
        d, mon, y = m1.groups()
        mon_lower = mon[:3].lower()
        if mon_lower in MONTH_MAP:
            mo = MONTH_MAP[mon_lower]
            year = f"20{y}" if len(y) == 2 else y
            return f"{int(year):04d}-{mo}-{int(d):02d}"

    m2 = re.match(r'^([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{2,4})$', s)
    if m2:
        mon, d, y = m2.groups()
        mon_lower = mon[:3].lower()
        if mon_lower in MONTH_MAP:
            mo = MONTH_MAP[mon_lower]
            year = f"20{y}" if len(y) == 2 else y
            return f"{int(year):04d}-{mo}-{int(d):02d}"

    m3 = re.match(r'^(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2,4})$', s)
    if m3:
        d, mo, y = m3.groups()
        year = f"20{y}" if len(y) == 2 else y
        return f"{int(year):04d}-{int(mo):02d}-{int(d):02d}"

    return s

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
    if "BOUNCE_FAILURE" not in flags and not sip_or_trading:
        if credit >= 10000 and any(k in d for k in ["INSTALOAN", "INSTA LOAN", "INSTAJUMBOLOAN", "JUMBO LOAN", "LOAN DISB", "DISBURSEMENT", "POONAWALLA"]):
            flags.append("LOAN_DISBURSEMENT")
        elif debit >= 200:
            is_bajaj_pay_merchant = "BAJAJPAY" in d or "@INDUS-INDB0MERCHA" in d
            emi_patterns = (
                r'(ACH\s*D[-_]|ACHDR|ACH-DR|NACH[-_]|BFLAUTOPAY|_EMI_|LOAN\s*REC|CANFIN|TATACAP|TCHHL|TCHIN|ADINGEN_TATA|CANARA\s*RO|HINDON)'
            )
            is_direct_emi = bool(re.search(emi_patterns, d)) or (
                any(k in d for k in ["KRAZYBEE", "POONAWALLAFIN", "NORTHERN ARC", "ADITYA BIRLA FINANCE", "MUTHOOT"])
            )
            if re.search(r'IB\s*BILLPAY\s*DR[-_\s]*HDFC', d) and re.search(r'\d{6}X{4,6}\d{4}', d):
                is_direct_emi = True
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

    return ", ".join(flags) if flags else "GENERAL"

def _infer_opening_balance(balance, amt, narration):
    n = narration.upper()
    if round(balance - amt, 2) < 0:
        return round(balance + amt, 2)
    credit_hint = any(k in n for k in ["NEFT CR", "NEFT-CR", "RTGS CR", "SALARY", "INT.PD", "INTEREST", "REFUND", "REV", "CREDIT"])
    return round(balance - amt, 2) if credit_hint else round(balance + amt, 2)

def _fill_period_from_records(metadata, records):
    if records and not metadata.get("period"):
        dates = sorted(r["iso_date"] for r in records if r.get("iso_date"))
        if dates:
            metadata["period"] = f"{dates[0]} to {dates[-1]}"
