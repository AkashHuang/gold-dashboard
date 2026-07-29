#!/usr/bin/env python3
"""黄金价格波动监控板 - 数据更新与提醒脚本

用法：
    python update.py

功能：
    1. 读取 config.json 中的品种配置
    2. 通过 market_data.py（腾讯公开行情 qt.gtimg.cn + 上海黄金交易所 sge.com.cn）拉取行情
    3. 更新本地 data/latest.json 与 index.html 监控板
    4. 较昨日收盘波动超阈值时，发送 Server酱 与邮件提醒
"""

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import market_data

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.json"
ALERTS_PATH = DATA_DIR / "alerts_pending.json"
HTML_PATH = ROOT / "index.html"

GITHUB_PAGES_PUBLISH_SCRIPT = Path("/Users/akash/.workbuddy/skills/github-pages-publish/scripts/push_dashboard.py")

BEIJING_TZ = timezone(timedelta(hours=8))


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------------
# 时间工具（北京时间）
# ----------------------------------------------------------------------------

def get_beijing_time() -> datetime:
    """返回当前北京时间。"""
    return datetime.now(BEIJING_TZ)


def is_active_now(active_hours: list, beijing_dt: datetime) -> bool:
    """判断当前北京时间是否落在配置的活跃时段内。

    active_hours: list of [start, end]，格式 "HH:MM"。
    支持跨午夜窗口（start > end），如 ["22:00", "02:30"]。
    空列表（未配置）视为全天活跃。
    """
    if not active_hours:
        return True
    t = beijing_dt.time()
    for window in active_hours:
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            continue
        try:
            start = datetime.strptime(window[0], "%H:%M").time()
            end = datetime.strptime(window[1], "%H:%M").time()
        except ValueError:
            continue
        if start <= end:
            if start <= t <= end:
                return True
        else:  # 跨午夜：如 22:00 - 02:30
            if t >= start or t <= end:
                return True
    return False


def is_china_gold_open(beijing_dt: datetime) -> bool:
    """判断上海黄金（AU99.99）是否处于交易时段（北京时间）。

    日盘：09:00-11:30, 13:30-15:30
    夜盘：20:00-次日 02:30
    """
    t = beijing_dt.time()
    morning = datetime.strptime("09:00", "%H:%M").time() <= t <= datetime.strptime("11:30", "%H:%M").time()
    afternoon = datetime.strptime("13:30", "%H:%M").time() <= t <= datetime.strptime("15:30", "%H:%M").time()
    night = t >= datetime.strptime("20:00", "%H:%M").time() or t <= datetime.strptime("02:30", "%H:%M").time()
    return morning or afternoon or night


# ----------------------------------------------------------------------------
# 取数 / 计算
# ----------------------------------------------------------------------------

def fetch_from_snapshot(inst: dict, snap: dict) -> dict:
    """从统一行情快照中取数（替代原 NeoData 拉取）。"""
    inst_id = inst["id"]
    if inst_id == "fx":
        data = snap.get("fx", {})
        usdcny = data.get("usdcny", {}) or {}
        if not isinstance(data, dict) or not isinstance(usdcny, dict) or usdcny.get("latest") is None:
            return {"_error": "行情获取失败或无数据"}
        # 保留嵌套结构，供渲染 / 夜盘估算使用
        return {"usdcny": usdcny}
    data = snap.get(inst_id)
    if not isinstance(data, dict) or data.get("latest") is None:
        return {"_error": "行情获取失败或无数据"}
    result = {}
    for k in ("latest", "prev_close", "open", "high", "low", "change_pct", "volume", "update_time", "source"):
        if k in data:
            result[k] = data[k]
    return result


def compute_night_gold(inst: dict, latest: dict, beijing_dt: datetime) -> dict:
    """上海黄金（夜盘估算）。

    中国市场休市：用 COMEX 黄金实时价 × USDCNY / 31.1035（实时跟踪外盘）
    中国市场开市：用 COMEX 黄金昨结价（收盘参考）× USDCNY / 31.1035（冻结为隔夜参考）
    """
    cg = latest.get("comex_gold", {})
    fx = latest.get("fx", {}).get("usdcny", {})
    cg_latest = cg.get("latest")
    cg_prev = cg.get("prev_close")
    usdcny_latest = fx.get("latest")
    usdcny_prev = fx.get("prev_close")
    if cg_latest is None or usdcny_latest is None:
        return {"_error": "缺少 COMEX 黄金或 USDCNY 行情"}
    if cg_prev is None or usdcny_prev is None:
        return {"_error": "缺少 COMEX 黄金昨结或 USDCNY 昨收"}

    open_market = is_china_gold_open(beijing_dt)
    if open_market:
        # 开市：用 COMEX 昨结（收盘参考）作为基准；USDCNY 用实时
        basis = cg_prev
        value = basis * usdcny_latest / 31.1035
        change_pct = None  # 冻结参考，不计算日内涨跌
        prev_value = basis * usdcny_prev / 31.1035
    else:
        # 休市：实时跟踪 COMEX 最新价
        value = cg_latest * usdcny_latest / 31.1035
        prev_value = cg_prev * usdcny_prev / 31.1035
        change_pct = (value - prev_value) / prev_value * 100 if prev_value else None

    # 日内高/低
    history = load_json(HISTORY_PATH, {})
    hist_inst = history.get("instruments", {}).get(inst["id"], {})
    high = hist_inst.get("high")
    low = hist_inst.get("low")
    if high is None or value > high:
        high = value
    if low is None or value < low:
        low = value

    return {
        "latest": value,
        "prev_close": prev_value,
        "change_pct": change_pct,
        "high": high,
        "low": low,
        "market_state": "开市(隔夜参考)" if open_market else "休市(实时)",
    }


def compute_formula(inst: dict, latest: dict) -> dict:
    """计算公式类品种。公式中可引用 {instrument_id}.latest 或 {instrument_id}.{subkey}.latest 等。"""
    formula = inst["formula"]
    result = {"_error": None}

    # 找出所有形如 word.word[.word...] 的引用
    pattern = r'[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+'
    refs = re.findall(pattern, formula)

    substitutions = {}
    for ref in refs:
        parts = ref.split(".")
        val = latest
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                val = None
                break
        if val is None:
            result["_error"] = f"无法解析引用: {ref}"
            return result
        substitutions[ref] = float(val)

    # 替换引用为数值
    expr = formula
    for ref, val in substitutions.items():
        expr = expr.replace(ref, str(val))

    try:
        value = eval(expr, {"__builtins__": {}}, {})
    except Exception as e:
        result["_error"] = f"公式计算失败: {e}"
        return result

    result["latest"] = value

    # 尝试用 prev_close 计算涨跌幅
    prev_substitutions = {}
    all_prev_available = True
    for ref in refs:
        parts = ref.split(".")
        # 把末尾的 latest 替换成 prev_close
        prev_parts = parts[:-1] + ["prev_close"] if parts[-1] == "latest" else parts
        val = latest
        for part in prev_parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                val = None
                break
        if val is None:
            all_prev_available = False
            break
        prev_substitutions[ref] = float(val)

    if all_prev_available:
        prev_expr = formula
        for ref, val in prev_substitutions.items():
            prev_expr = prev_expr.replace(ref, str(val))
        try:
            prev_value = eval(prev_expr, {"__builtins__": {}}, {})
            result["prev_close"] = prev_value
            if prev_value != 0:
                result["change_pct"] = (value - prev_value) / prev_value * 100
            else:
                result["change_pct"] = None
        except Exception:
            result["change_pct"] = None
    else:
        result["change_pct"] = None

    # 日内高/低
    history = load_json(HISTORY_PATH, {})
    hist_inst = history.get("instruments", {}).get(inst["id"], {})
    high = hist_inst.get("high")
    low = hist_inst.get("low")
    if high is None or value > high:
        high = value
    if low is None or value < low:
        low = value
    result["high"] = high
    result["low"] = low

    return result


def compute_ratio(inst: dict, latest: dict) -> dict:
    """计算比值类品种。"""
    num_id = inst["numerator"]
    den_id = inst["denominator"]
    num = latest.get(num_id, {}).get("latest")
    den = latest.get(den_id, {}).get("latest")
    num_prev = latest.get(num_id, {}).get("prev_close")
    den_prev = latest.get(den_id, {}).get("prev_close")

    result = {"_error": None}
    if num is None or den is None or den == 0:
        result["_error"] = f"缺少 {num_id} 或 {den_id} 的价格"
        return result

    ratio = num / den
    result["latest"] = ratio

    # 计算比值较昨日收盘的涨跌幅
    if num_prev is not None and den_prev is not None and den_prev != 0:
        prev_ratio = num_prev / den_prev
        result["prev_close"] = prev_ratio
        result["change_pct"] = (ratio - prev_ratio) / prev_ratio * 100
    else:
        result["change_pct"] = None

    # 日内高/低：从 history 里跟踪，没有则取当前
    history = load_json(HISTORY_PATH, {})
    hist_inst = history.get("instruments", {}).get(inst["id"], {})
    high = hist_inst.get("high")
    low = hist_inst.get("low")
    if high is None or ratio > high:
        high = ratio
    if low is None or ratio < low:
        low = ratio
    result["high"] = high
    result["low"] = low

    return result


def update_history(latest: dict) -> None:
    """更新历史高/低等跟踪数据。"""
    history = load_json(HISTORY_PATH, {"instruments": {}, "date": None})
    today = datetime.now().strftime("%Y-%m-%d")
    if history.get("date") != today:
        history = {"instruments": {}, "date": today}

    for inst_id, data in latest.items():
        if inst_id.startswith("_"):
            continue
        hist = history["instruments"].get(inst_id, {})
        for field in ("high", "low"):
            val = data.get(field)
            if val is None:
                continue
            if hist.get(field) is None or val > hist.get(field):
                hist[field] = val
            if hist.get(field) is None or val < hist.get(field):
                hist[field] = val
        history["instruments"][inst_id] = hist

    save_json(HISTORY_PATH, history)


def format_change_pct(value: float | None) -> str:
    if value is None:
        return "--"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def format_price(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "--"
    return f"{value:,.{decimals}f}"


def format_int(value: int | None) -> str:
    if value is None:
        return "--"
    return f"{value:,}"


def _parse_alert_time(alert: dict) -> datetime | None:
    """解析 alerts_log 条目的 time 字段为 datetime，解析失败返回 None（保留）。"""
    t = alert.get("time")
    if not t:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%m-%d %H:%M"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None  # 无法解析 → 保留，不误删


def send_serverchan(sendkeys: list[str], title: str, body: str) -> None:
    for key in sendkeys:
        url = f"https://sctapi.ftqq.com/{key}.send"
        data = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except Exception as e:
            print(f"Server酱发送失败 ({key[:8]}...): {e}", file=sys.stderr)


def build_alert_messages(config: dict, latest: dict, triggered: list[tuple]) -> list[dict]:
    """构造待发送的提醒消息列表。"""
    alerts = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    for inst_id, change_pct in triggered:
        inst = next((i for i in config["instruments"] if i["id"] == inst_id), None)
        if not inst:
            continue
        data = latest.get(inst_id, {})
        latest_price = data.get("latest")
        title = f"【{inst['name']}】波动 {format_change_pct(change_pct)}"
        body_lines = [
            f"时间：{now_str}",
            f"品种：{inst['name']}",
            f"最新价：{format_price(latest_price)}",
            f"较昨收：{format_change_pct(change_pct)}",
            f"阈值：{inst.get('threshold', config.get('default_threshold', 1.0))}%",
        ]
        body = "\n".join(body_lines)
        alerts.append({"title": title, "body": body, "inst_id": inst_id, "change_pct": change_pct})
    return alerts


def comex_settlement_dates(months: int = 12) -> list[dict]:
    """计算 COMEX 黄金期货未来 N 个月的最后交易日（倒数第三个工作日，仅排除周末）。"""
    results = []
    today = datetime.now()
    month_codes = ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]
    for offset in range(months):
        year = today.year + (today.month - 1 + offset) // 12
        month = (today.month - 1 + offset) % 12 + 1
        # 取月末
        if month == 12:
            next_month = datetime(year + 1, 1, 1)
        else:
            next_month = datetime(year, month + 1, 1)
        last_day = next_month - timedelta(days=1)

        # 倒数第三个工作日（仅周末）
        workdays_count = 0
        current = last_day
        while workdays_count < 3:
            if current.weekday() < 5:
                workdays_count += 1
            if workdays_count < 3:
                current -= timedelta(days=1)

        code = f"GC{month_codes[month - 1]}{str(year)[-2:]}"
        results.append({
            "year": year,
            "month": month,
            "code": code,
            "last_trading_day": current.strftime("%Y-%m-%d")
        })
    return results


def render_dashboard(config: dict, latest: dict, alerts_log: list, run_time: datetime) -> str:
    """渲染 HTML 监控板。"""
    title = config["dashboard"].get("title", "黄金价格波动监控板")
    refresh = run_time.strftime(config["dashboard"].get("refresh_time_format", "%Y-%m-%d %H:%M"))
    settlements = comex_settlement_dates()

    cards_html = []
    for inst in config["instruments"]:
        if not inst.get("enabled", True):
            continue
        data = latest.get(inst["id"], {})

        name = inst["name"]
        unit = inst.get("unit", "")
        threshold = inst.get("threshold", config.get("default_threshold", 1.0))
        volume_unit = inst.get("volume_unit", "")
        # 主价格与涨跌幅
        if inst["id"] == "fx":
            usdcny = data.get("usdcny", {}) or {}
            latest_price = usdcny.get("latest")
            change_pct = usdcny.get("change_pct")
            high = usdcny.get("high")
            low = usdcny.get("low")
            prev_close = usdcny.get("prev_close")
            volume = None
            market_state = None
        else:
            latest_price = data.get("latest")
            change_pct = data.get("change_pct")
            high = data.get("high")
            low = data.get("low")
            prev_close = data.get("prev_close")
            volume = data.get("volume")
            market_state = data.get("market_state")

        change_color = "#97c459" if (change_pct is not None and change_pct >= 0) else "#f09595"
        change_sign = "+" if (change_pct is not None and change_pct >= 0) else ""
        change_text = f"{change_sign}{change_pct:.2f}%" if change_pct is not None else "--"

        # 副信息行：昨收/成交量/昨结
        if data.get("_error"):
            sub_info = "数据暂不可用"
        elif inst["id"] == "fx":
            sub_info = ""  # 价格已在主行展示，无需重复
        elif volume is not None:
            vol_label = f" {volume_unit}" if volume_unit else ""
            sub_info = f"成交量 {format_int(int(volume))}{vol_label}"
        elif prev_close is not None:
            sub_info = f"昨收 {format_price(prev_close)}"
        else:
            sub_info = ""

        if market_state:
            sub_info = (sub_info + " · " if sub_info else "") + market_state

        unit_label = f" / {unit}" if unit else ""
        header = f"{name}{unit_label}"

        cards_html.append(f"""
        <div class="card">
          <div class="card-left">
            <div class="card-header">
              <span class="inst-name">{header}</span>
              <span class="threshold">阈值 {threshold}%</span>
            </div>
            <div class="main-price" style="color: {change_color};">
              {format_price(latest_price)} <span class="change">{change_text}</span>
            </div>
            <div class="sub-info">{sub_info}</div>
          </div>
          <div class="card-right">
            <div><span class="hl-label">高</span> <span class="high-price">{format_price(high)}</span></div>
            <div><span class="hl-label">低</span> <span class="low-price">{format_price(low)}</span></div>
          </div>
        </div>
        """)

    alerts_html = []
    for alert in alerts_log[-20:]:
        cls = "alert-triggered" if alert.get("triggered") else "alert-normal"
        alerts_html.append(f'<div class="{cls}">{alert["time"]} {alert["message"]}</div>')
    if not alerts_html:
        alerts_html.append('<div class="alert-normal">暂无波动提醒记录</div>')

    settlement_html = []
    for s in settlements[:6]:
        settlement_html.append(f'<div>{s["month"]}月 {s["code"]} · {s["last_trading_day"]}</div>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #1c1c1a;
      --surface: #262626;
      --text: #e8e6e1;
      --text-secondary: #b4b2a9;
      --text-tertiary: #888780;
      --red: #f09595;
      --green: #97c459;
      --border: rgba(240,238,231,0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 1.25rem;
    }}
    .container {{ max-width: 720px; margin: 0 auto; }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1rem;
    }}
    h1 {{ margin: 0; font-size: 15px; font-weight: 500; }}
    .refresh-time {{ font-size: 12px; color: var(--text-secondary); }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 1rem;
    }}
    .card {{
      background: var(--surface);
      border-radius: 8px;
      padding: 1rem;
      display: flex;
      justify-content: space-between;
      align-items: stretch;
      border: 0.5px solid var(--border);
    }}
    .card-left {{
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-width: 0;
      flex: 1;
    }}
    .card-header {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      width: 100%;
    }}
    .inst-name {{ font-size: 12px; color: var(--text-secondary); }}
    .threshold {{ font-size: 12px; color: var(--text-tertiary); }}
    .main-price {{
      font-size: 24px;
      font-weight: 500;
      white-space: nowrap;
      margin-top: 4px;
    }}
    .change {{ font-size: 13px; }}
    .sub-info {{
      font-size: 12px;
      color: var(--text-tertiary);
      margin-top: 4px;
    }}
    .card-right {{
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: flex-end;
      text-align: right;
      font-size: 12px;
      line-height: 1.7;
      margin-left: 12px;
      color: var(--text-secondary);
    }}
    .hl-label {{ color: var(--text-tertiary); }}
    .high-price {{ color: var(--red); }}
    .low-price {{ color: var(--green); }}
    .bottom {{
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 12px;
    }}
    .panel {{
      background: var(--surface);
      border-radius: 8px;
      padding: 1rem;
      border: 0.5px solid var(--border);
    }}
    .panel-title {{
      font-size: 13px;
      font-weight: 500;
      margin-bottom: 8px;
    }}
    .panel-content {{
      font-size: 12px;
      color: var(--text-secondary);
      line-height: 1.8;
    }}
    .alert-triggered {{ color: var(--red); }}
    .alert-normal {{ color: var(--text-secondary); }}
    @media (max-width: 600px) {{
      .cards {{ grid-template-columns: 1fr; }}
      .bottom {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>{title}</h1>
      <span class="refresh-time">更新：{refresh}</span>
    </div>
    <div class="cards">
      {''.join(cards_html)}
    </div>
    <div class="bottom">
      <div class="panel">
        <div class="panel-title">近期波动记录</div>
        <div class="panel-content">
          {''.join(alerts_html)}
        </div>
      </div>
      <div class="panel">
        <div class="panel-title">COMEX 结算日历</div>
        <div class="panel-content">
          {''.join(settlement_html)}
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""
    return html


def publish_github_pages(config: dict) -> None:
    """如果配置启用，将 index.html 推送到 GitHub Pages。"""
    # 在 GitHub Actions 中由工作流通过 git commit 发布，跳过本地 Contents API 推送
    if os.environ.get("GITHUB_ACTIONS"):
        print("GitHub Pages publish skipped in CI (handled by workflow git push).")
        return
    gp = config.get("publish", {}).get("github_pages", {})
    if not gp.get("enabled"):
        return
    repo = gp.get("repo")
    path = gp.get("path", "index.html")
    msg = gp.get("commit_message", "Update dashboard")
    if not repo:
        print("GitHub Pages publish skipped: repo not configured")
        return
    if not GITHUB_PAGES_PUBLISH_SCRIPT.exists():
        print(f"GitHub Pages publish script not found: {GITHUB_PAGES_PUBLISH_SCRIPT}")
        return
    cmd = [
        str(sys.executable),
        str(GITHUB_PAGES_PUBLISH_SCRIPT),
        "--repo", repo,
        "--path", path,
        "--html", str(HTML_PATH),
        "--msg", msg,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        print(f"GitHub Pages publish failed: {proc.stdout} {proc.stderr}")
    else:
        print(f"GitHub Pages publish OK: {proc.stdout.strip()}")


def main() -> int:
    config = load_config()
    # CI 环境下，提醒密钥从 GitHub Actions Secrets 注入（避免提交到公开仓库）
    if os.environ.get("GITHUB_ACTIONS"):
        sc = os.environ.get("SERVERCHAN_SENDKEY")
        if sc:
            config.setdefault("notifiers", {}).setdefault("serverchan", {})["sendkeys"] = [sc]
        em = os.environ.get("ALERT_EMAILS")
        if em:
            config.setdefault("notifiers", {}).setdefault("email", {})["recipients"] = [
                x.strip() for x in em.split(",") if x.strip()
            ]
    now = datetime.now()
    beijing_now = get_beijing_time()

    # 活跃时段守卫：不在窗口内则跳过整轮刷新（保留旧数据，避免误报）
    active_hours = config.get("active_hours")
    if not is_active_now(active_hours, beijing_now):
        print(f"当前北京时间 {beijing_now:%H:%M} 不在活跃时段 {active_hours}，跳过刷新。")
        return 0

    # 读取上一轮快照（用于推算涨跌幅 / 历史高低压缩）
    prev_snapshot = load_json(LATEST_PATH, {})
    snap = market_data.get_snapshot(prev_snapshot)

    # 两遍：先 data，再 ratio/computed（确保依赖可用）
    latest: dict = {"_meta": {"updated_at": now.isoformat()}}
    for inst in config["instruments"]:
        if not inst.get("enabled", True):
            continue
        if inst["type"] == "data":
            try:
                latest[inst["id"]] = fetch_from_snapshot(inst, snap)
            except Exception as e:
                latest[inst["id"]] = {"_error": str(e)}
    for inst in config["instruments"]:
        if not inst.get("enabled", True):
            continue
        if inst["type"] == "ratio":
            try:
                latest[inst["id"]] = compute_ratio(inst, latest)
            except Exception as e:
                latest[inst["id"]] = {"_error": str(e)}
        elif inst["type"] == "computed":
            try:
                if inst["id"] == "shanghai_gold_night":
                    latest[inst["id"]] = compute_night_gold(inst, latest, beijing_now)
                else:
                    latest[inst["id"]] = compute_formula(inst, latest)
            except Exception as e:
                latest[inst["id"]] = {"_error": str(e)}

    # 更新历史高/低
    update_history(latest)

    # 判断提醒
    cooldown = timedelta(hours=config.get("alert_cooldown_hours", 4))
    alerts_log = load_json(DATA_DIR / "alerts_log.json", [])
    triggered: list[tuple[str, float]] = []

    for inst in config["instruments"]:
        if not inst.get("enabled", True):
            continue
        inst_id = inst["id"]
        data = latest.get(inst_id, {})
        if data.get("_error"):
            continue
        change_pct = data.get("change_pct")
        threshold = inst.get("threshold", config.get("default_threshold", 1.0))
        if change_pct is None or threshold is None or threshold <= 0:
            continue
        if abs(change_pct) < threshold:
            alerts_log.append({
                "time": now.strftime("%Y-%m-%d %H:%M"),
                "inst_id": inst_id,
                "message": f"{inst['name']} {format_change_pct(change_pct)} · 未触发",
                "triggered": False
            })
            continue

        # 检查是否在冷却期内已经提醒过
        last_alert = None
        for a in reversed(alerts_log):
            if a.get("inst_id") == inst_id and a.get("triggered"):
                last_alert = a
                break

        if last_alert:
            try:
                last_time = datetime.strptime(last_alert["time"], "%Y-%m-%d %H:%M")
            except ValueError:
                last_time = None
            if last_time and now - last_time < cooldown:
                continue

        triggered.append((inst_id, change_pct))
        alerts_log.append({
            "time": now.strftime("%Y-%m-%d %H:%M"),
            "inst_id": inst_id,
            "message": f"{inst['name']} {format_change_pct(change_pct)} · 已触发",
            "triggered": True
        })

    # 发送 Server酱
    pending_alerts = build_alert_messages(config, latest, triggered)
    if pending_alerts and config.get("notifiers", {}).get("serverchan", {}).get("enabled"):
        keys = config["notifiers"]["serverchan"].get("sendkeys", [])
        for alert in pending_alerts:
            send_serverchan(keys, alert["title"], alert["body"])

    # 邮件提醒写入 pending，由 WorkBuddy automation 通过 agent-mail 发送
    save_json(ALERTS_PATH, pending_alerts)

    # 保存 alerts_log（按保留窗口清理 + 截断上限 100 条）
    retention_hours = config.get("record_retention_hours", 24)
    if retention_hours > 0:
        cutoff = now - timedelta(hours=retention_hours)
        alerts_log = [
            a for a in alerts_log
            if not _parse_alert_time(a) or _parse_alert_time(a) >= cutoff
        ]
    alerts_log = alerts_log[-100:]
    save_json(DATA_DIR / "alerts_log.json", alerts_log)

    # 保存最新数据
    save_json(LATEST_PATH, latest)

    # 渲染 HTML
    html = render_dashboard(config, latest, alerts_log, now)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    # 推送到 GitHub Pages
    publish_github_pages(config)

    print(f"Updated at {now.strftime('%Y-%m-%d %H:%M:%S')} (北京时间 {beijing_now:%H:%M})")
    print(f"Instruments: {len(config['instruments'])}, Alerts pending: {len(pending_alerts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
