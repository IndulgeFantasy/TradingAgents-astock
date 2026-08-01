from langchain_core.tools import tool
from typing import Annotated
import re
from tradingagents.dataflows.interface import route_to_vendor

_A_STOCK_CODE_RE = re.compile(r"^\d{6}$")


def _invalid_a_stock_code_message(tool_name: str, ticker: str) -> str:
    return (
        f"Invalid ticker for `{tool_name}`: {ticker!r}. "
        "This tool only accepts a 6-digit A-stock code, not Chinese text, "
        "company names, sector names, concepts, or search keywords. "
        "Use the original analysis ticker/code in the tool call."
    )


def _validate_a_stock_code(tool_name: str, ticker: str) -> tuple[bool, str]:
    code = str(ticker or "").strip()
    if not _A_STOCK_CODE_RE.fullmatch(code):
        return False, _invalid_a_stock_code_message(tool_name, code)
    return True, code


@tool
def get_news(
    ticker: Annotated[str, "6-digit A-stock code (e.g. 600379). Must be numeric, NOT company name or Chinese text"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve news data for a given stock code.
    Uses the configured news_data vendor.
    Args:
        ticker (str): 6-digit A-stock code, e.g. 600379, 300750. Must be the numeric code, not the company name.
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted string containing news data
    """
    ok, code_or_message = _validate_a_stock_code("get_news", ticker)
    if not ok:
        return code_or_message
    return route_to_vendor("get_news", code_or_message, start_date, end_date)

@tool
def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int, "Number of days to look back"] = 7,
    limit: Annotated[int, "Maximum number of articles to return"] = 5,
) -> str:
    """
    Retrieve global news data.
    Uses the configured news_data vendor.
    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Number of days to look back (default 7)
        limit (int): Maximum number of articles to return (default 5)
    Returns:
        str: A formatted string containing global news data
    """
    return route_to_vendor("get_global_news", curr_date, look_back_days, limit)

@tool
def get_insider_transactions(
    ticker: Annotated[str, "6-digit A-stock code (e.g. 600379). Must be numeric, NOT company name"],
) -> str:
    """
    Retrieve company events (公司大事) from 同花顺F10 event.html.
    Returns: 近期重要事件(财报披露/公告/融资融券/大宗交易/业绩预告/股东大会/分红/回购/股权质押/股东增减持等),
    高管持股变动(变动日期/变动人/与高管关系/变动数量/均价/剩余股数/变动途径),
    股东持股变动(公告日期/变动股东/变动数量/均价/剩余股份/变动期间/途径),
    担保明细(担保金额/期限/担保方/类型/被担保方),
    违规处理(公告日期/处罚金额/处罚类型/处理人/处罚对象/违规行为/处罚说明),
    机构调研(机构类别+调研机构名称列表).
    Args:
        ticker (str): 6-digit A-stock code, e.g. 600379
    Returns:
        str: Company events report
    """
    ok, code_or_message = _validate_a_stock_code("get_insider_transactions", ticker)
    if not ok:
        return code_or_message
    return route_to_vendor("get_insider_transactions", code_or_message)
