#!/usr/bin/env python3
"""
台股強勢股盤後篩選腳本
條件：
1. 收盤價 15~100 元，漲幅 >= 7%
2. 收盤價 > 20MA，今日量 >= 前一交易日 5 日均量 * 2
3. 外資買超 > 0 或 投信買超 > 0
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests
import yfinance as yf

# --- 常數 ---
TW_TZ = timezone(timedelta(hours=8))
PRICE_MIN = 15.0
PRICE_MAX = 100.0
CHANGE_PCT_MIN = 7.0
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def tw_now() -> datetime:
    return datetime.now(TW_TZ)


def to_float(value: Any) -> float | None:
    """將字串/數字轉為 float，失敗回傳 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in {"", "-", "--", "---", "X", "x", "nan", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_sign_from_html(html: str) -> int:
    """解析 MI_INDEX 漲跌欄位中的 +/- 符號。"""
    text = str(html)
    if "+" in text or "color:red" in text.lower():
        return 1
    if "-" in text or "color:green" in text.lower():
        return -1
    return 0


def recent_trading_dates(max_lookback: int = 14) -> list[str]:
    """由近到遠產生可能的交易日 YYYYMMDD（略過週末）。"""
    dates: list[str] = []
    day = tw_now().date()
    for _ in range(max_lookback * 2):
        if day.weekday() < 5:
            dates.append(day.strftime("%Y%m%d"))
        day -= timedelta(days=1)
        if len(dates) >= max_lookback:
            break
    return dates


def fetch_mi_index(date_str: str) -> list[dict[str, Any]]:
    """
    抓取證交所每日收盤行情 MI_INDEX（個股）。
    優先使用 rwd API；失敗時改用 OpenAPI STOCK_DAY_ALL 作為備援。
    """
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    params = {"date": date_str, "type": "ALLBUT0999", "response": "json"}
    try:
        resp = SESSION.get(url, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("stat") != "OK":
            raise ValueError(f"MI_INDEX stat={payload.get('stat')}")

        stock_table = None
        for table in payload.get("tables", []):
            title = str(table.get("title", ""))
            fields = table.get("fields") or []
            if "每日收盤行情" in title or (
                "證券代號" in fields and "收盤價" in fields and "漲跌價差" in fields
            ):
                stock_table = table
                break

        if not stock_table:
            raise ValueError("找不到每日收盤行情表格")

        fields = stock_table["fields"]
        rows = stock_table.get("data") or []
        results: list[dict[str, Any]] = []
        for row in rows:
            if not row or len(row) < len(fields):
                continue
            item = dict(zip(fields, row))
            code = str(item.get("證券代號", "")).strip()
            # 僅保留一般上市普通股（4 碼數字）
            if not re.fullmatch(r"[1-9]\d{3}", code):
                continue

            close = to_float(item.get("收盤價"))
            change_abs = to_float(item.get("漲跌價差"))
            if close is None or change_abs is None or close <= 0:
                continue

            sign = parse_sign_from_html(str(item.get("漲跌(+/-)", "")))
            signed_change = sign * abs(change_abs)
            prev_close = close - signed_change
            if prev_close <= 0:
                continue
            change_pct = (signed_change / prev_close) * 100.0

            results.append(
                {
                    "code": code,
                    "name": str(item.get("證券名稱", "")).strip(),
                    "close": close,
                    "change_pct": round(change_pct, 2),
                    "source_date": date_str,
                }
            )
        if results:
            print(f"[INFO] MI_INDEX {date_str} 取得 {len(results)} 檔個股")
            return results
        raise ValueError("MI_INDEX 無有效個股資料")
    except Exception as exc:
        print(f"[WARN] MI_INDEX {date_str} 失敗: {exc}，改用 OpenAPI STOCK_DAY_ALL")
        return fetch_stock_day_all_openapi()


def fetch_stock_day_all_openapi() -> list[dict[str, Any]]:
    """備援：OpenAPI 上市個股日成交資訊。"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    results: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("Code", "")).strip()
        if not re.fullmatch(r"[1-9]\d{3}", code):
            continue
        close = to_float(row.get("ClosingPrice"))
        change = to_float(row.get("Change"))
        if close is None or change is None or close <= 0:
            continue
        prev_close = close - change
        if prev_close <= 0:
            continue
        change_pct = (change / prev_close) * 100.0
        results.append(
            {
                "code": code,
                "name": str(row.get("Name", "")).strip(),
                "close": close,
                "change_pct": round(change_pct, 2),
                "source_date": str(row.get("Date", "")),
            }
        )
    print(f"[INFO] OpenAPI STOCK_DAY_ALL 取得 {len(results)} 檔個股")
    return results


def preliminary_screen(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """初步篩選：收盤價 15~100，漲幅 >= 7%。"""
    selected = []
    for s in stocks:
        if PRICE_MIN <= s["close"] <= PRICE_MAX and s["change_pct"] >= CHANGE_PCT_MIN:
            selected.append(s)
    print(f"[INFO] 初步篩選後剩餘 {len(selected)} 檔")
    return selected


def fetch_institutional_map(date_str: str) -> dict[str, dict[str, float]]:
    """
    抓取三大法人買賣超（單位：張）。
    回傳 {code: {foreign_net, trust_net}}
    """
    url = "https://www.twse.com.tw/rwd/zh/fund/T86"
    params = {
        "date": date_str,
        "selectType": "ALLBUT0999",
        "response": "json",
    }
    resp = SESSION.get(url, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("stat") != "OK":
        raise ValueError(f"T86 stat={payload.get('stat')}")

    fields = payload.get("fields") or []
    data = payload.get("data") or []
    result: dict[str, dict[str, float]] = {}

    for row in data:
        if not row or len(row) < len(fields):
            continue
        item = dict(zip(fields, row))
        code = str(item.get("證券代號", "")).strip()
        if not re.fullmatch(r"[1-9]\d{3}", code):
            continue

        # 外資（不含自營商）買賣超股數
        foreign_shares = to_float(item.get("外陸資買賣超股數(不含外資自營商)"))
        trust_shares = to_float(item.get("投信買賣超股數"))
        if foreign_shares is None:
            foreign_shares = 0.0
        if trust_shares is None:
            trust_shares = 0.0

        result[code] = {
            "foreign_net": round(foreign_shares / 1000.0, 0),  # 張
            "trust_net": round(trust_shares / 1000.0, 0),
        }

    print(f"[INFO] T86 {date_str} 取得 {len(result)} 檔法人資料")
    return result


def pass_technical_filter(code: str) -> bool:
    """
    yfinance 技術條件：
    - 今日收盤 > 20MA
    - 今日成交量 >= 前一交易日的 5 日均量 * 2
    """
    ticker = f"{code}.TW"
    try:
        hist = yf.download(
            ticker,
            period="2mo",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if hist is None or hist.empty:
            print(f"[WARN] {ticker} 無歷史資料")
            return False

        # yfinance 多 ticker 時可能是 MultiIndex columns
        if isinstance(hist.columns, pd.MultiIndex):
            hist.columns = hist.columns.get_level_values(0)

        hist = hist.dropna(subset=["Close", "Volume"])
        if len(hist) < 21:
            print(f"[WARN] {ticker} 歷史資料不足 ({len(hist)} 天)")
            return False

        close = float(hist["Close"].iloc[-1])
        volume = float(hist["Volume"].iloc[-1])
        ma20 = float(hist["Close"].rolling(20).mean().iloc[-1])

        # 前一交易日當下的 5 日均量 = 不含今日、往前 5 日的均量
        vol_ma5_prev = float(hist["Volume"].iloc[-6:-1].mean())

        if pd.isna(ma20) or pd.isna(vol_ma5_prev) or vol_ma5_prev <= 0:
            return False

        cond_ma = close > ma20
        cond_vol = volume >= vol_ma5_prev * 2
        ok = cond_ma and cond_vol
        print(
            f"[DEBUG] {ticker} close={close:.2f} ma20={ma20:.2f} "
            f"vol={volume:.0f} vol5prev={vol_ma5_prev:.0f} -> {ok}"
        )
        return ok
    except Exception as exc:
        print(f"[WARN] {ticker} 技術篩選失敗: {exc}")
        return False


def load_mi_index_with_fallback() -> tuple[list[dict[str, Any]], str]:
    """嘗試近期交易日，直到取得 MI_INDEX 資料。"""
    last_error: Exception | None = None
    for date_str in recent_trading_dates(10):
        try:
            stocks = fetch_mi_index(date_str)
            if stocks:
                return stocks, date_str
        except Exception as exc:
            last_error = exc
            print(f"[WARN] 日期 {date_str} 無法取得行情: {exc}")
            time.sleep(1.2)
    if last_error:
        raise last_error
    raise RuntimeError("無法取得任何交易日收盤行情")


def load_institutional_with_fallback(preferred_date: str) -> tuple[dict[str, dict[str, float]], str]:
    """優先使用行情日期的法人資料；盤後剛開盤時可能延遲，先重試再往前找。"""
    last_error: Exception | None = None

    # 對目標日期多重試（配合 15:30 排程，法人報表偶發延遲）
    for attempt in range(1, 4):
        try:
            data = fetch_institutional_map(preferred_date)
            if data:
                return data, preferred_date
        except Exception as exc:
            last_error = exc
            print(f"[WARN] T86 {preferred_date} 第 {attempt} 次失敗: {exc}")
            time.sleep(3 * attempt)

    for date_str in recent_trading_dates(10):
        if date_str == preferred_date:
            continue
        try:
            data = fetch_institutional_map(date_str)
            if data:
                print(f"[WARN] 改用較早法人日期 {date_str}（目標日 {preferred_date} 尚無資料）")
                return data, date_str
        except Exception as exc:
            last_error = exc
            print(f"[WARN] T86 {date_str} 失敗: {exc}")
            time.sleep(1.2)
    if last_error:
        raise last_error
    raise RuntimeError("無法取得三大法人買賣超資料")

def build_output(stocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "last_updated": tw_now().strftime("%Y-%m-%d %H:%M:%S"),
        "stocks": stocks,
    }


def main() -> None:
    print("[INFO] 開始執行台股強勢股篩選...")
    output_path = "data.json"

    try:
        all_stocks, quote_date = load_mi_index_with_fallback()
        candidates = preliminary_screen(all_stocks)

        if not candidates:
            payload = build_output([])
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print("[INFO] 初步篩選無標的，已寫入空清單")
            return

        try:
            institutional, inst_date = load_institutional_with_fallback(quote_date)
            print(f"[INFO] 使用法人資料日期: {inst_date}")
        except Exception as exc:
            print(f"[ERROR] 法人資料取得失敗: {exc}")
            institutional = {}

        final_stocks: list[dict[str, Any]] = []
        for stock in candidates:
            code = stock["code"]
            try:
                # 籌碼：外資買超 > 0 或 投信買超 > 0
                inst = institutional.get(code, {"foreign_net": 0.0, "trust_net": 0.0})
                foreign_net = float(inst.get("foreign_net", 0.0))
                trust_net = float(inst.get("trust_net", 0.0))
                if not (foreign_net > 0 or trust_net > 0):
                    print(f"[SKIP] {code} 法人未買超 (外資={foreign_net}, 投信={trust_net})")
                    continue

                # 技術面
                if not pass_technical_filter(code):
                    print(f"[SKIP] {code} 未通過技術條件")
                    continue

                lot_cost = round(stock["close"] * 1000)  # 一張 = 1000 股
                final_stocks.append(
                    {
                        "code": code,
                        "name": stock["name"],
                        "close": stock["close"],
                        "change_pct": stock["change_pct"],
                        "foreign_net": int(foreign_net),
                        "trust_net": int(trust_net),
                        "lot_cost": lot_cost,
                    }
                )
                print(f"[PASS] {code} {stock['name']}")
                # 避免對 yfinance 請求過快
                time.sleep(0.4)
            except Exception as exc:
                print(f"[WARN] 處理 {code} 時發生錯誤，已略過: {exc}")
                continue

        # 依漲幅由高到低排序
        final_stocks.sort(key=lambda x: x["change_pct"], reverse=True)
        payload = build_output(final_stocks)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        print(f"[INFO] 完成！符合條件 {len(final_stocks)} 檔，已寫入 {output_path}")
        print(f"[INFO] last_updated = {payload['last_updated']}")
    except Exception as exc:
        # 即使整體失敗，也寫入空資料避免前端壞掉
        print(f"[ERROR] 篩選流程失敗: {exc}")
        payload = build_output([])
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as write_exc:
            print(f"[ERROR] 無法寫入 {output_path}: {write_exc}")
        raise


if __name__ == "__main__":
    main()
