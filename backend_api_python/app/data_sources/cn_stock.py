"""
中国A股数据源 — 多层 fallback

有 TWELVE_DATA_API_KEY:
  所有周期 → Twelve Data（主） → 腾讯日/周线 → yfinance → AkShare

无 API Key:
  分钟/小时 → yfinance → AkShare
  日/周线 → 腾讯 fqkline → yfinance → AkShare
"""

from __future__ import annotations

from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.tencent import normalize_cn_code, fetch_quote, parse_quote_to_ticker, fetch_kline, tencent_kline_rows_to_dicts
from app.data_sources.asia_stock_kline import (
    normalize_chart_timeframe,
    fetch_twelvedata_klines,
    fetch_yfinance_klines,
    fetch_akshare_minute_klines,
    fetch_akshare_weekly_klines,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _apply_split_adjustment(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Forward-adjust prices across share-split discontinuities.

    The Tencent ``fqkline`` endpoint returns *unadjusted* ``day`` bars for some
    ETFs when their qfq series is unavailable (e.g. 512480 after its 1:2 split
    on 2026-07-02). Without adjustment the chart shows a fake ~50% crash at the
    split. This helper detects those discontinuities and scales the historical
    bars so the series is continuous (aligned to the latest price, i.e. 前复权).

    Detection: an adjacent gap where the next bar opens well below the previous
    bar's close (ratio < 0.75) and the level holds afterwards — a classic
    share-split signature rather than a routine daily move.
    """
    if not bars or len(bars) < 4:
        return bars
    bars = sorted(bars, key=lambda b: (b.get("time") or 0))
    result = list(bars)
    # Process oldest -> newest so multiple splits are all aligned to the latest.
    for i in range(len(result) - 1):
        prev_close = _to_float(result[i].get("close"))
        next_open = _to_float(result[i + 1].get("open"))
        if prev_close <= 0 or next_open <= 0:
            continue
        ratio = next_open / prev_close
        # Split signature: price drops by >=25% overnight AND holds afterwards.
        if ratio >= 0.75:
            continue
        if not _level_holds(result, i + 1):
            continue
        # Forward-adjust everything at or before the split by the drop ratio.
        for j in range(0, i + 1):
            for key in ("open", "high", "low", "close"):
                v = _to_float(result[j].get(key))
                result[j][key] = round(v * ratio, 6) if v else v
        logger.info("CNStock: applied forward split adjustment ratio=%.4f at bar idx=%s", ratio, i)
    return result


def _level_holds(bars: List[Dict[str, Any]], start_idx: int) -> bool:
    """Return True if the level right after a suspected split holds for a few bars."""
    base = _to_float(bars[start_idx].get("open"))
    if base <= 0:
        return False
    checked = 0
    for k in range(start_idx + 1, min(start_idx + 5, len(bars))):
        close = _to_float(bars[k].get("close"))
        if close <= 0:
            continue
        checked += 1
        # Held if it stays within +/-35% of the post-split open (allows real
        # subsequent moves but excludes a quick bounce back to the old price).
        if abs(close - base) / base > 0.35:
            return False
    return checked >= 2


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class CNStockDataSource(BaseDataSource):
    """A股数据源（TwelveData + Tencent + yfinance + AkShare）"""

    name = "CNStock/multi-source"

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        code = normalize_cn_code(symbol)
        parts = fetch_quote(code)
        if not parts:
            return {"last": 0, "symbol": code}
        t = parse_quote_to_ticker(parts)
        return {
            "last": t.get("last", 0),
            "change": t.get("change", 0),
            "changePercent": t.get("changePercent", 0),
            "high": t.get("high", 0),
            "low": t.get("low", 0),
            "open": t.get("open", 0),
            "previousClose": t.get("previousClose", 0),
            "name": t.get("name", ""),
            "symbol": code,
        }

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        code = normalize_cn_code(symbol)
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # Tier 1: Twelve Data (paid, most reliable)
        rows = fetch_twelvedata_klines(
            is_hk=False, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            return self.filter_and_limit(
                rows,
                limit=lim,
                before_time=before_time,
                after_time=after_time,
                truncate=(after_time is None),
            )

        # Tier 2: Tencent for daily/weekly (fast, free)
        if tf in ("1D", "1W"):
            tf_map = {"1D": "day", "1W": "week"}
            period = tf_map.get(tf, "day")
            raw_rows = fetch_kline(code, period=period, count=lim, adj="qfq")
            out = tencent_kline_rows_to_dicts(raw_rows)
            # Tencent may return unadjusted bars for ETFs without a qfq series;
            # forward-adjust across any share-split discontinuities.
            out = _apply_split_adjustment(out)
            if out:
                return self.filter_and_limit(
                    out,
                    limit=lim,
                    before_time=before_time,
                    after_time=after_time,
                    truncate=(after_time is None),
                )

        # Tier 3: yfinance (works when Yahoo not rate-limited)
        rows = fetch_yfinance_klines(
            is_hk=False, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            return self.filter_and_limit(
                rows,
                limit=lim,
                before_time=before_time,
                after_time=after_time,
                truncate=(after_time is None),
            )

        # Tier 4: AkShare (fragile overseas, last resort)
        if tf in ("1m", "5m", "15m", "30m", "1H", "4H"):
            rows = fetch_akshare_minute_klines(
                is_hk=False, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
            )
        elif tf == "1W":
            rows = fetch_akshare_weekly_klines(
                is_hk=False, tencent_code=code, limit=lim, before_time=before_time
            )
        else:
            rows = []

        return self.filter_and_limit(
            rows,
            limit=lim,
            before_time=before_time,
            after_time=after_time,
            truncate=(after_time is None),
        )
