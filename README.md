# test

## DCF analysis tool

This repository includes a standalone discounted cash flow script:

- `/home/runner/work/test/test/dcf_tool.py`

### What it does

Given a ticker symbol, it:

1. Fetches the company CIK from SEC ticker mappings.
2. Retrieves the latest 3 annual 10-K filings from SEC submissions.
3. Pulls historical cash flow facts from SEC XBRL company facts.
4. Estimates WACC using CAPM (with Yahoo Finance beta/market data and SEC tax/debt inputs).
5. Projects free cash flow for 5 years.
6. Calculates enterprise value, equity value, and estimated price per share.

### Usage

```bash
python3 /home/runner/work/test/test/dcf_tool.py AAPL --pretty
```

Optional flags:

- `--terminal-growth` (default `0.025`)
- `--risk-free-rate` (default `0.04`)
- `--market-return` (default `0.09`)
