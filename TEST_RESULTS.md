# Local regression test results

The five sample bank statements supplied in this conversation were run through the parser and underwriting pipeline. The source PDFs are deliberately excluded from this project archive because they contain private financial information.

| Sample | Bank detected | Transactions | Aggregate variance | Running-balance breaks | ABB days | Result |
|---|---|---:|---:|---:|---:|---|
| `Jun.pdf` | Standard Chartered | 15 | ₹0.00 | 0 | 30 | Reconciled |
| `Icici bank statement.pdf` | ICICI | 13 | ₹0.00 | 0 | 2 | Reconciled |
| `hdfc.pdf` | HDFC | 501 | ₹0.00 | 0 | 222 | Reconciled |
| `axisbank.pdf` | Axis | 35 | ₹0.00 | 0 | 184 | Reconciled |
| `icicibank.pdf` | ICICI | 267 | ₹0.00 | 0 | 184 | Reconciled |

Additional checks:

- `python -m py_compile analyzer_engine.py server.py bank_analyzer.py export_service.py`: passed.
- `python -m pytest -q`: 11 tests passed.
- FastAPI upload, dashboard, transaction-list, Excel export, LOS JSON export, and reset tested against all five sample PDFs.
- New upload replaces the previous analysis in the same browser session.
- Separate browser sessions did not see each other's uploaded statements; exports without that session's statement returned 404.
- Excel exports returned valid XLSX bytes; LOS JSON exports returned valid JSON.
- Inline frontend JavaScript passed `node --check`.
- Gemini live generation was not tested because the local environment had no Google Gen AI SDK/API key. Gemini remains optional; deterministic analysis and exports work without it.

These results validate only the supplied samples. They do not guarantee every bank template will parse correctly and do not authenticate a PDF as genuinely bank-issued.
