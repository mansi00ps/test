#!/usr/bin/env python3
import argparse
import json
import statistics
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


SEC_USER_AGENT = "DCF-Analysis-Tool/1.0 (opensource@example.com)"
DEFAULT_GROWTH_RATE = 0.05
DEFAULT_COST_OF_DEBT = 0.05
DEFAULT_TAX_RATE = 0.21


@dataclass
class Filing:
    form: str
    filing_date: str
    accession_number: str
    primary_document: str
    url: str


def fetch_json(url: str, headers: Optional[Dict[str, str]] = None) -> Dict:
    request_headers = {"User-Agent": SEC_USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_cik_for_ticker(ticker: str) -> str:
    data = fetch_json("https://www.sec.gov/files/company_tickers.json")
    ticker = ticker.upper()
    for record in data.values():
        if record.get("ticker", "").upper() == ticker:
            return str(record["cik_str"]).zfill(10)
    raise ValueError(f"Ticker '{ticker}' not found in SEC mapping.")


def get_last_10k_filings(cik: str, count: int = 3) -> List[Filing]:
    data = fetch_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    filings = []
    cik_no_leading = str(int(cik))
    for i, form in enumerate(forms):
        if form == "10-K":
            accession = accessions[i]
            accession_no_dash = accession.replace("-", "")
            primary_doc = docs[i]
            filing_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_no_leading}/"
                f"{accession_no_dash}/{primary_doc}"
            )
            filings.append(
                Filing(
                    form=form,
                    filing_date=dates[i],
                    accession_number=accession,
                    primary_document=primary_doc,
                    url=filing_url,
                )
            )
        if len(filings) >= count:
            break
    if len(filings) < count:
        raise ValueError(f"Found only {len(filings)} recent 10-K filings for CIK {cik}.")
    return filings


def get_company_facts(cik: str) -> Dict:
    return fetch_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")


def get_usd_series(facts: Dict, concept_names: List[str]) -> List[Tuple[str, float]]:
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    for name in concept_names:
        concept = us_gaap.get(name, {})
        units = concept.get("units", {})
        usd_values = units.get("USD", [])
        if not usd_values:
            continue
        annual = [
            item
            for item in usd_values
            if item.get("form") == "10-K" and item.get("val") is not None and item.get("fy")
        ]
        if not annual:
            continue
        annual_sorted = sorted(annual, key=lambda x: int(x.get("fy", 0)))
        deduped = {}
        for item in annual_sorted:
            deduped[item["fy"]] = float(item["val"])
        return [(str(year), value) for year, value in sorted(deduped.items())]
    return []


def pick_latest(series: List[Tuple[str, float]]) -> Optional[float]:
    if not series:
        return None
    return series[-1][1]


def estimate_growth_rate(
    fcf_history: List[float], max_growth_rate: float = 0.2, min_growth_rate: float = -0.1
) -> float:
    if len(fcf_history) < 2:
        return DEFAULT_GROWTH_RATE
    growth_rates = []
    for i in range(1, len(fcf_history)):
        prev = fcf_history[i - 1]
        curr = fcf_history[i]
        if abs(prev) < 1e-9:
            continue
        growth_rates.append((curr - prev) / abs(prev))
    if not growth_rates:
        return DEFAULT_GROWTH_RATE
    avg = statistics.mean(growth_rates)
    return max(min_growth_rate, min(max_growth_rate, avg))


def project_fcfs(base_fcf: float, growth_rate: float, years: int = 5) -> List[float]:
    fcfs = []
    current = base_fcf
    for _ in range(years):
        current *= 1 + growth_rate
        fcfs.append(current)
    return fcfs


def discount_cash_flows(cash_flows: List[float], discount_rate: float) -> float:
    total = 0.0
    for i, cf in enumerate(cash_flows, start=1):
        total += cf / ((1 + discount_rate) ** i)
    return total


def fetch_yahoo_quote_summary(ticker: str) -> Dict:
    url = (
        "https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
        f"{ticker}?modules=defaultKeyStatistics,financialData,price"
    )
    data = fetch_json(url, headers={"User-Agent": "Mozilla/5.0"})
    result = data.get("quoteSummary", {}).get("result")
    if not result:
        raise ValueError(f"Yahoo Finance data unavailable for ticker '{ticker}'.")
    return result[0]


def raw_value(item: Optional[Dict]) -> Optional[float]:
    if not isinstance(item, dict):
        return None
    value = item.get("raw")
    if value is None:
        return None
    return float(value)


def compute_wacc(
    equity_value: float,
    debt_value: float,
    beta: float,
    interest_expense: Optional[float],
    tax_rate: float,
    risk_free_rate: float,
    market_return: float,
) -> Tuple[float, float, float]:
    total_capital = equity_value + debt_value
    if total_capital <= 0:
        raise ValueError("Cannot compute WACC with non-positive total capital.")
    equity_weight = equity_value / total_capital
    debt_weight = debt_value / total_capital
    cost_of_equity = risk_free_rate + beta * (market_return - risk_free_rate)
    if debt_value > 0 and interest_expense and interest_expense > 0:
        cost_of_debt = interest_expense / debt_value
    else:
        cost_of_debt = DEFAULT_COST_OF_DEBT
    wacc = (equity_weight * cost_of_equity) + (debt_weight * cost_of_debt * (1 - tax_rate))
    return wacc, cost_of_equity, cost_of_debt


def calculate_price_per_share(
    projected_fcfs: List[float],
    wacc: float,
    terminal_growth: float,
    debt: float,
    cash: float,
    shares_outstanding: float,
) -> Tuple[float, float, float]:
    if wacc <= terminal_growth:
        raise ValueError("WACC must be greater than terminal growth rate.")
    pv_fcfs = discount_cash_flows(projected_fcfs, wacc)
    terminal_fcf = projected_fcfs[-1] * (1 + terminal_growth)
    terminal_value = terminal_fcf / (wacc - terminal_growth)
    pv_terminal = terminal_value / ((1 + wacc) ** len(projected_fcfs))
    enterprise_value = pv_fcfs + pv_terminal
    equity_value = enterprise_value - debt + cash
    if shares_outstanding <= 0:
        raise ValueError("Shares outstanding must be positive.")
    return enterprise_value, equity_value, equity_value / shares_outstanding


def compute_tax_rate(facts: Dict) -> float:
    income_tax = pick_latest(
        get_usd_series(facts, ["IncomeTaxExpenseBenefit", "IncomeTaxExpense"])
    )
    pretax_income = pick_latest(get_usd_series(facts, ["IncomeBeforeTax"]))
    if income_tax is None or pretax_income is None or abs(pretax_income) < 1e-9:
        return DEFAULT_TAX_RATE
    rate = income_tax / pretax_income
    return max(0.0, min(0.4, rate))


def build_fcf_history(facts: Dict, years: int = 3) -> List[Tuple[str, float]]:
    cfo_series = get_usd_series(
        facts,
        [
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ],
    )
    capex_series = get_usd_series(
        facts,
        ["PaymentsToAcquirePropertyPlantAndEquipment"],
    )
    if not cfo_series or not capex_series:
        raise ValueError("Could not retrieve operating cash flow and capex from company facts.")

    cfo_map = {year: value for year, value in cfo_series}
    capex_map = {year: value for year, value in capex_series}
    common_years = sorted(set(cfo_map) & set(capex_map))
    if len(common_years) < years:
        raise ValueError(f"Need at least {years} annual data points for FCF history.")
    selected_years = common_years[-years:]
    history = []
    for year in selected_years:
        cfo = cfo_map[year]
        # Normalize CapEx to outflow magnitude because filings may report it as positive or negative.
        capex = abs(capex_map[year])
        fcf = cfo - capex
        history.append((year, fcf))
    return history


def run_analysis(
    ticker: str,
    terminal_growth: float,
    risk_free_rate: float,
    market_return: float,
) -> Dict:
    cik = get_cik_for_ticker(ticker)
    filings = get_last_10k_filings(cik, count=3)
    facts = get_company_facts(cik)
    fcf_history_pairs = build_fcf_history(facts, years=3)
    fcf_history = [value for _, value in fcf_history_pairs]

    quote = fetch_yahoo_quote_summary(ticker)
    stats = quote.get("defaultKeyStatistics", {})
    financial_data = quote.get("financialData", {})
    price_data = quote.get("price", {})

    shares_outstanding = raw_value(stats.get("sharesOutstanding")) or raw_value(
        price_data.get("sharesOutstanding")
    )
    market_cap = raw_value(price_data.get("marketCap"))
    beta = raw_value(stats.get("beta"))
    if beta is None:
        beta = 1.0
        print(
            "Warning: beta unavailable from Yahoo Finance; using fallback beta=1.0.",
            file=sys.stderr,
        )
    debt = raw_value(financial_data.get("totalDebt")) or 0.0
    cash = raw_value(financial_data.get("totalCash")) or 0.0

    if shares_outstanding is None or market_cap is None:
        raise ValueError("Missing market cap or shares outstanding from Yahoo Finance.")

    interest_expense = pick_latest(get_usd_series(facts, ["InterestExpense"]))
    tax_rate = compute_tax_rate(facts)
    growth_rate = estimate_growth_rate(fcf_history)
    projected_fcfs = project_fcfs(fcf_history[-1], growth_rate, years=5)

    wacc, cost_of_equity, cost_of_debt = compute_wacc(
        equity_value=market_cap,
        debt_value=debt,
        beta=beta,
        interest_expense=interest_expense,
        tax_rate=tax_rate,
        risk_free_rate=risk_free_rate,
        market_return=market_return,
    )
    enterprise_value, equity_value, estimated_price = calculate_price_per_share(
        projected_fcfs=projected_fcfs,
        wacc=wacc,
        terminal_growth=terminal_growth,
        debt=debt,
        cash=cash,
        shares_outstanding=shares_outstanding,
    )

    return {
        "ticker": ticker.upper(),
        "cik": cik,
        "ten_k_filings": [filing.__dict__ for filing in filings],
        "historical_fcf": [{"year": year, "fcf": value} for year, value in fcf_history_pairs],
        "assumptions": {
            "risk_free_rate": risk_free_rate,
            "market_return": market_return,
            "terminal_growth_rate": terminal_growth,
            "tax_rate": tax_rate,
            "beta": beta,
            "growth_rate": growth_rate,
            "cost_of_equity": cost_of_equity,
            "cost_of_debt": cost_of_debt,
            "wacc": wacc,
        },
        "valuation": {
            "projected_fcfs": projected_fcfs,
            "enterprise_value": enterprise_value,
            "equity_value": equity_value,
            "estimated_price_per_share": estimated_price,
            "market_cap_reference": market_cap,
            "debt": debt,
            "cash": cash,
            "shares_outstanding": shares_outstanding,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "DCF valuation tool: fetches recent 10-K filings and estimates intrinsic price per share."
        )
    )
    parser.add_argument("ticker", help="Ticker symbol (e.g., AAPL)")
    parser.add_argument(
        "--terminal-growth",
        type=float,
        default=0.025,
        help="Terminal growth rate used in Gordon Growth (default: 0.025)",
    )
    parser.add_argument(
        "--risk-free-rate",
        type=float,
        default=0.04,
        help="Risk-free rate for CAPM/WACC (default: 0.04)",
    )
    parser.add_argument(
        "--market-return",
        type=float,
        default=0.09,
        help="Expected market return for CAPM/WACC (default: 0.09)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output",
    )
    args = parser.parse_args()

    try:
        result = run_analysis(
            ticker=args.ticker,
            terminal_growth=args.terminal_growth,
            risk_free_rate=args.risk_free_rate,
            market_return=args.market_return,
        )
        if args.pretty:
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps(result))
        return 0
    except (ValueError, urllib.error.URLError, TimeoutError) as err:
        print(f"Error while analyzing ticker '{args.ticker}': {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
