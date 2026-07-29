#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一行情获取模块（腾讯公开行情 qt.gtimg.cn + 上海黄金交易所 sge.com.cn）。

零第三方依赖，纯标准库。返回结构化快照，供 update.py 直接使用。
覆盖：COMEX 黄金/白银期货、伦敦金/银现货、上海黄金/白银（SGE）、美元兑人民币。
"""

import json
import re
import datetime
import urllib.request
import urllib.parse
import urllib.error

# ----------------------------------------------------------------------------
# 1) 腾讯公开行情接口：外盘贵金属 + 外汇
# ----------------------------------------------------------------------------

GTIMG_URL = "https://qt.gtimg.cn/q={codes}"
GTIMG_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"}

GTIMG_CODES = {
    "comex_gold": "fuGC",      # COMEX 黄金期货
    "comex_silver": "fuSI",    # COMEX 白银期货
    "london_gold": "hf_XAU",   # 伦敦金现货
    "london_silver": "hf_XAG", # 伦敦银现货
    "fx_usdcny": "fxUSDCNY",   # 美元/人民币
}

# ----------------------------------------------------------------------------
# 2) 上海黄金交易所官网接口：上海金 / 上海白银
# ----------------------------------------------------------------------------

SGE_URL = "https://www.sge.com.cn/graph/quotations"
SGE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.sge.com.cn/",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
SGE_CODES = {
    "shanghai_gold": "Au99.99",   # 上海黄金
    "shanghai_silver": "Ag99.99", # 上海白银（单位：元/千克）
}


def _to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("--", "").strip())
    except (ValueError, AttributeError):
        return None


def _http_get(url, headers=None, data=None, method="GET", timeout=15, retries=3, backoff=1.5):
    """带重试的 HTTP GET/POST，规避公开行情接口偶发超时/连接重置。"""
    import time
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return raw.decode("gbk", errors="replace")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_err or RuntimeError("unknown http error")


def _fetch_gtimg_raw(codes):
    url = GTIMG_URL.format(codes=",".join(codes))
    return _http_get(url, headers=GTIMG_HEADERS, timeout=15, retries=3)


def _parse_future(body):
    """解析 fuGC/fuSI 波浪号分隔期货行情。"""
    parts = body.split("~")
    if len(parts) < 6:
        return None
    latest = _to_float(parts[3])
    prev_close = _to_float(parts[4])   # 昨结
    open_ = _to_float(parts[5])
    volume = _to_float(parts[6])        # 成交量（手）
    # 时间字段之后的 涨跌 / 涨跌% / 最高 / 最低
    change_pct = high = low = None
    for i, p in enumerate(parts):
        if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", p):
            try:
                change_pct = _to_float(parts[i + 2])
                high = _to_float(parts[i + 3])
                low = _to_float(parts[i + 4])
            except (IndexError, ValueError):
                pass
            break
    return {
        "latest": latest, "prev_close": prev_close, "open": open_,
        "high": high, "low": low, "change_pct": change_pct, "volume": volume,
    }


def _parse_spot(body):
    """解析 hf_XAU/hf_XAG 逗号分隔现货行情（仅含最新价，涨跌幅用历史推算）。"""
    parts = [p.strip() for p in body.split(",")]
    if not parts:
        return None
    return {"latest": _to_float(parts[0])}


def _parse_fx(body):
    """解析 fxUSDCNY 波浪号分隔外汇行情。"""
    parts = body.split("~")
    if len(parts) < 14:
        return None
    return {
        "latest": _to_float(parts[3]),
        "prev_close": _to_float(parts[6]),
        "open": _to_float(parts[7]),
        "high": _to_float(parts[8]),
        "low": _to_float(parts[9]),
        "change_pct": _to_float(parts[13]),
    }


def fetch_gtimg():
    result = {}
    rev = {code: key for key, code in GTIMG_CODES.items()}
    try:
        raw = _fetch_gtimg_raw(list(GTIMG_CODES.values()))
        for line in raw.split(";"):
            line = line.strip()
            if not line.startswith("v_"):
                continue
            var, _, val = line.partition("=")
            code = var[2:]
            key = rev.get(code)
            if not key or not val:
                continue
            body = val.strip().strip('"')
            if code.startswith("fu"):
                data = _parse_future(body)
            elif code.startswith("hf"):
                data = _parse_spot(body)
            elif code.startswith("fx"):
                data = _parse_fx(body)
            else:
                data = None
            if data:
                data["source"] = "qt.gtimg.cn"
                result[key] = data
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        for key in GTIMG_CODES:
            result[key] = {"latest": None, "source": "qt.gtimg.cn", "error": str(e)}
    return result


def fetch_sge():
    result = {}
    for key, instid in SGE_CODES.items():
        try:
            data = urllib.parse.urlencode({"instid": instid}).encode("utf-8")
            raw = _http_get(SGE_URL, headers=SGE_HEADERS, data=data, method="POST", timeout=15, retries=3)
            j = json.loads(raw)
            series = j.get("data") or []
            price = _to_float(series[-1]) if series else None
            result[key] = {
                "latest": price,
                "high": _to_float(j.get("max")),
                "low": _to_float(j.get("min")),
                "update_time": j.get("delaystr"),
                "source": "sge.com.cn",
            }
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError, IndexError) as e:
            result[key] = {"latest": None, "source": "sge.com.cn", "error": str(e)}
    return result


def get_snapshot(prev_snapshot=None):
    """获取全部指标快照。prev_snapshot 用于补充历史涨跌幅（来源未直接提供时）。

    返回结构与 update.py 的 latest.json 保持一致：
      { instrument_id: {latest, prev_close, open, high, low, change_pct, volume, ...},
        "fx": {"usdcny": {...}, "dxy": None},
        "_meta": {...} }
    """
    snap = {}
    snap.update(fetch_gtimg())
    snap.update(fetch_sge())

    # fx 包装成旧结构 {usdcny: {...}, dxy: None}
    fx_raw = snap.pop("fx_usdcny", {})
    snap["fx"] = {"usdcny": fx_raw, "dxy": None}

    # 对来源未直接提供 change_pct 的品种（伦敦现货、SGE），用上一次快照推算
    if prev_snapshot:
        for key, data in snap.items():
            if not isinstance(data, dict):
                continue
            if data.get("change_pct") is not None or data.get("latest") is None:
                continue
            if key == "fx":
                usd = data.get("usdcny", {}) or {}
                if usd.get("change_pct") is not None:
                    continue
                pu = (prev_snapshot.get("fx", {}).get("usdcny", {}) or {}).get("latest")
                cu = usd.get("latest")
                if pu:
                    usd["change_pct"] = (cu - pu) / pu * 100
                continue
            prev = prev_snapshot.get(key, {})
            prev_latest = prev.get("latest") if isinstance(prev, dict) else None
            if prev_latest:
                data["change_pct"] = (data["latest"] - prev_latest) / prev_latest * 100

    snap["_meta"] = {"update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    return snap


if __name__ == "__main__":
    print(json.dumps(get_snapshot(), ensure_ascii=False, indent=2))
