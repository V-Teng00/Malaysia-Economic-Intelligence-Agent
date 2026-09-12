"""
Malaysia Economic Data MCP Server.
Custom tools over public Malaysian economic data (data.gov.my, api.bnm.gov.my)
"""

import os
from datetime import datetime
from typing import Optional

import httpx
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("malaysia-econ-data", version="0.1.0")

BASE_URL = "https://api.data.gov.my/data-catalogue"
EXCHANGE_RATES_DATASET_MIN_DATE = "2003-01-01"

BNM_BASE_URL = "https://api.bnm.gov.my/public"
BNM_HEADERS = {"Accept": "application/vnd.BNM.API.v1+json"}  # required by BNM's API


async def _fetch_dataset(dataset_id: str, extra_params: Optional[dict] = None) -> list[dict]:
    """Query data.gov.my with server-side filters (see developer.data.gov.my/request-query)."""
    params = {"id": dataset_id, **(extra_params or {})}
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(BASE_URL, params=params)
        resp.raise_for_status()
        return resp.json()


async def _fetch_bnm(path: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0, headers=BNM_HEADERS, follow_redirects=True) as client:
        resp = await client.get(f"{BNM_BASE_URL}/{path}")
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_myr_exchange_rate(
    currency_code: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    indicator: str = "avg",
) -> dict:
    """
    Get MYR exchange rate history against a currency (data.gov.my,
    monthly frequency, CC BY 4.0). Covers 2003-01-01 onward.

    Args:
        currency_code: 3-letter code, e.g. 'USD', 'SGD', 'EUR'.
        start_date: Optional 'YYYY-MM-DD'.
        end_date: Optional 'YYYY-MM-DD'.
        indicator: One of 'high', 'low', 'avg', 'start', 'end'. These are
            NOT interchangeable — don't mix indicators in one comparison.

    Returns:
        dict: currency_code, indicator, records ([{date, rate}]),
        pct_change_over_period, out_of_range (True if start_date predates
        2003-01-01), data_source.
    """
    currency_code = currency_code.lower()
    out_of_range = bool(start_date and start_date < EXCHANGE_RATES_DATASET_MIN_DATE)

    params = {"ifilter": f"{indicator}@indicator", "sort": "date", "limit": 5000}
    if start_date:
        params["date_start"] = f"{start_date}@date"
    if end_date:
        params["date_end"] = f"{end_date}@date"

    raw = await _fetch_dataset("exchangerates", params)
    records = [{"date": r["date"], "rate": r[currency_code]} for r in raw if currency_code in r]

    pct_change = None
    if len(records) >= 2 and records[0]["rate"]:
        pct_change = round((records[-1]["rate"] - records[0]["rate"]) / records[0]["rate"] * 100, 3)

    return {
        "currency_code": currency_code,
        "indicator": indicator,
        "records": records,
        "pct_change_over_period": pct_change,
        "out_of_range": out_of_range,
        "data_source": "Bank Negara Malaysia via data.gov.my (CC BY 4.0), dataset id=exchangerates",
    }


@mcp.tool()
async def get_interest_rate_history(
    rate_type: str,
    bank: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """
    Get Malaysian interest rate history (data.gov.my, CC BY 4.0). Covers
    fixed-deposit rates and BLR/ALR/SR — NOT OPR (use get_opr_history).

    Args:
        rate_type: exact 'rate' label, e.g. 'fdr_1mo', 'blr'. If unsure,
            check `available_rate_types` in the response.
        bank: optional, e.g. 'commercial' or 'investment'. None = all banks.
        start_date: optional 'YYYY-MM-DD'.
        end_date: optional 'YYYY-MM-DD'.

    Returns:
        dict: rate_type, bank_filter, records ([{date, bank, value}]),
        available_rate_types, found, data_source.
    """
    probe = await _fetch_dataset("interestrates", {"sort": "date", "limit": 2000})
    available_rate_types = sorted({row["rate"] for row in probe if "rate" in row})

    filter_parts = [f"{rate_type}@rate"]
    if bank:
        filter_parts.append(f"{bank}@bank")
    params = {"ifilter": ",".join(filter_parts), "sort": "date", "limit": 5000}
    if start_date:
        params["date_start"] = f"{start_date}@date"
    if end_date:
        params["date_end"] = f"{end_date}@date"

    raw = await _fetch_dataset("interestrates", params)
    records = [{"date": r["date"], "bank": r.get("bank"), "value": r["value"]} for r in raw]

    return {
        "rate_type": rate_type,
        "bank_filter": bank,
        "records": records,
        "available_rate_types": available_rate_types,
        "found": len(records) > 0,
        "data_source": "Bank Negara Malaysia via data.gov.my (CC BY 4.0), dataset id=interestrates",
    }


@mcp.tool()
async def get_opr_history(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """
    Get Bank Negara Malaysia's Overnight Policy Rate (OPR) history
    (api.bnm.gov.my, official public API). OPR only changes at Monetary
    Policy Committee meetings — a gap between records means no change
    happened, not missing data.

    Args:
        start_date: Optional 'YYYY-MM-DD'. If both dates are omitted,
            returns only the single latest decision, not a history.
        end_date: Optional 'YYYY-MM-DD'.

    Returns:
        dict: records ([{date, new_opr_level, change_in_opr}]),
        is_latest_only, data_source.
    """
    if start_date is None and end_date is None:
        payload = await _fetch_bnm("opr")
        d = payload["data"]
        return {
            "records": [{
                "date": d["date"],
                "new_opr_level": d["new_opr_level"],
                "change_in_opr": d["change_in_opr"],
            }],
            "is_latest_only": True,
            "data_source": "Bank Negara Malaysia public Open API (api.bnm.gov.my/public/opr)",
        }

    start_year = int(start_date[:4]) if start_date else int(end_date[:4])
    end_year = int(end_date[:4]) if end_date else datetime.now().year
    if start_year > end_year:
        start_year, end_year = end_year, start_year

    records: list[dict] = []
    for year in range(start_year, end_year + 1):
        payload = await _fetch_bnm(f"opr/year/{year}")
        year_data = payload["data"]
        if isinstance(year_data, dict):
            year_data = [year_data]
        records.extend(year_data)

    filtered = [
        r for r in records
        if (start_date is None or r["date"] >= start_date)
        and (end_date is None or r["date"] <= end_date)
    ]
    filtered.sort(key=lambda r: r["date"])

    return {
        "records": [{
            "date": r["date"],
            "new_opr_level": r["new_opr_level"],
            "change_in_opr": r["change_in_opr"],
        } for r in filtered],
        "is_latest_only": False,
        "data_source": "Bank Negara Malaysia public Open API (api.bnm.gov.my/public/opr)",
    }


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("MCP_PORT", "8000")),
    )