from analyzer_engine import compute_underwriting_insights, extract_dynamic_employer


def test_same_day_transactions_are_ordered_by_running_balance():
    records = [
        {
            "txn_date": "02-01-2026", "iso_date": "2026-01-02",
            "narration": "UPI DEBIT", "debit": 100.0, "credit": 0.0,
            "balance": 100.0, "category": "GENERAL", "bank": "Test Bank",
        },
        {
            "txn_date": "02-01-2026", "iso_date": "2026-01-02",
            "narration": "NEFT CREDIT", "debit": 0.0, "credit": 100.0,
            "balance": 200.0, "category": "GENERAL", "bank": "Test Bank",
        },
    ]
    metadata = {
        "bank": "Test Bank", "opening_balance": 100.0,
        "closing_balance": 100.0, "period": "01-01-2026 to 02-01-2026",
    }

    result = compute_underwriting_insights(records, metadata)

    assert records[0]["credit"] == 100.0
    assert records[1]["debit"] == 100.0
    assert result["reconciliation"]["balance_continuity_breaks"] == 0
    assert result["reconciliation"]["is_reconciled"] is True


def test_aggregate_match_does_not_hide_row_balance_error():
    records = [{
        "txn_date": "02-01-2026", "iso_date": "2026-01-02",
        "narration": "NEFT CREDIT", "debit": 0.0, "credit": 100.0,
        "balance": 199.0, "category": "GENERAL", "bank": "Test Bank",
    }]
    metadata = {
        "bank": "Test Bank", "opening_balance": 100.0,
        "closing_balance": 200.0, "period": "01-01-2026 to 02-01-2026",
    }

    result = compute_underwriting_insights(records, metadata)

    assert result["reconciliation"]["variance"] == 0.0
    assert result["reconciliation"]["aggregate_reconciled"] is True
    assert result["reconciliation"]["balance_continuity_breaks"] == 1
    assert result["reconciliation"]["is_reconciled"] is False
    assert result["reconciliation"]["status"] == "WARNING"


def test_single_salary_credit_does_not_get_prime_stability_score():
    records = [{
        "txn_date": "30-01-2026", "iso_date": "2026-01-30",
        "narration": "NEFT ACME PRIVATE LIMITED SALARY", "debit": 0.0,
        "credit": 50000.0, "balance": 50000.0, "category": "SALARY",
        "bank": "Test Bank",
    }]
    metadata = {
        "bank": "Test Bank", "opening_balance": 0.0,
        "closing_balance": 50000.0, "period": "01-01-2026 to 31-01-2026",
    }

    result = compute_underwriting_insights(records, metadata)

    assert result["salary_audit"]["observed_salary_months"] == 1
    assert result["salary_audit"]["stability_score"] < 65
    assert result["debt_and_emi"]["foir_available"] is True


def test_employer_code_prefix_is_removed():
    assert extract_dynamic_employer(
        "A2AINT01-ZIVORE APPAREL PRIVATE LIMITED-0000001565796284 SALARY-MAR-26"
    ) == "Zivore Apparel Private Limited"


def test_gemini_is_optional_when_sdk_is_unavailable(monkeypatch):
    import analyzer_engine as engine
    monkeypatch.setattr(engine, "GENAI_AVAILABLE", False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert engine.run_gemini_underwriting_intelligence([], {}) is None
