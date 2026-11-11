import analyzer_engine as engine


def test_parse_iso_date_supported_formats_and_invalid_date():
    assert engine.parse_iso_date("23.05.2026") == "2026-05-23"
    assert engine.parse_iso_date("02/04/26") == "2026-04-02"
    assert engine.parse_iso_date("30 Jun 2025") == "2025-06-30"
    assert engine.parse_iso_date("2026-13-40") == ""


def test_upi_transactions_have_a_distinct_category():
    assert engine.categorize_transaction(
        "UPI/SHOP/merchant@bank/Payment", debit=125.0, credit=0.0
    ) == "UPI_PAYMENT"


def test_same_day_transactions_are_ordered_by_running_balance():
    rows = [
        {"iso_date": "2026-05-01", "txn_date": "01.05.2026", "debit": 20, "credit": 0, "balance": 130},
        {"iso_date": "2026-05-01", "txn_date": "01.05.2026", "debit": 0, "credit": 50, "balance": 150},
    ]
    ordered = engine.order_transactions_by_balance(rows, 100)
    assert ordered[0]["credit"] == 50
    assert ordered[1]["debit"] == 20


def test_abb_includes_statement_days_before_first_transaction():
    rows = [
        {"iso_date": "2026-04-02", "txn_date": "02/04/2026", "balance": 150},
        {"iso_date": "2026-04-03", "txn_date": "03/04/2026", "balance": 120},
    ]
    abb = engine.calculate_daily_abb(rows, 100, 120, "01/04/2026 to 03/04/2026")
    assert abb["days_tracked"] == 3
    assert abb["overall_abb"] == 123.33


def test_foir_is_marked_unavailable_without_tagged_salary():
    rows = [
        {"bank": "Test Bank", "txn_date": "01/04/2026", "iso_date": "2026-04-01",
         "narration": "NACH EMI", "debit": 1000.0, "credit": 0.0,
         "balance": 9000.0, "category": "EMI_LOAN"},
    ]
    metadata = {"bank": "Test Bank", "opening_balance": 10000.0, "closing_balance": 9000.0,
                "period": "01/04/2026 to 01/04/2026"}
    insights = engine.compute_underwriting_insights(rows, metadata)
    assert insights["reconciliation"]["status"] == "RECONCILED"
    assert insights["debt_and_emi"]["foir_available"] is False


def test_ai_redaction_removes_common_direct_identifiers():
    cleaned = engine._redact_for_ai("NEFT 9876543210 user.name@upi ABCDE1234F HDFC0001234")
    assert "9876543210" not in cleaned
    assert "user.name@upi" not in cleaned
    assert "ABCDE1234F" not in cleaned
