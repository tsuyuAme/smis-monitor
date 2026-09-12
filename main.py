#!/usr/bin/env python3
"""
SMIS 挂刀监控
- 打开 smis.club/exchange
- 拦截 API 数据 + 解析表格（双通道）
- 按挂刀比例 / 7日跌幅 / 成交量过滤
- 新发现发 Telegram，7 天后复核提醒

本地调试（Windows PowerShell）:
  $env:TG_BOT_TOKEN = "xxx"
  $env:TG_CHAT_ID = "xxx"
  $env:SMIS_HEADED = "1"          # 弹出浏览器窗口
  $env:SMIS_MANUAL_WAIT = "60"    # 有验证码时给你 60 秒手动点
  python main.py
"""

import json
import os
import time
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "state.json"
CONFIG_FILE = BASE_DIR / "config.json"
DEBUG_DIR = BASE_DIR / "data" / "debug"
BROWSER_DIR = BASE_DIR / "browser_data"
UTC = timezone.utc


def now_utc():
    return datetime.now(UTC)


def parse_number(text):
    if text is None:
        return None
    s = str(text).strip().replace(",", "").replace("¥", "").replace("$", "").replace("￥", "")
    if not s or s in {"-", "--", "N/A", "暂无", "No Data"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def parse_percent(text):
    if text is None:
        return None
    s = str(text).strip().replace("%", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def normalize_ratio(text):
    if text is None:
        return None
    v = parse_number(text)
    if v is None:
        return None
    return v / 100 if v > 1.5 else v


def load_json(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")


def get_config():
    cfg = load_json(CONFIG_FILE, {})
    env_override = os.getenv("SMIS_CONFIG_JSON")
    if env_override:
        cfg.update(json.loads(env_override))
    return cfg


# ---------------------------------------------------------------------------
# 从 API JSON 解析（优先）
# ---------------------------------------------------------------------------
def items_from_api_payload(payload, _depth=0):
    """解析 smis /api/commodity/exchange 返回。"""
    if payload is None:
        return []

    candidates = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            for k in ("list", "records", "rows", "items", "result"):
                if isinstance(data.get(k), list):
                    candidates = data[k]
                    break
            if not candidates:
                # 递归浅搜
                for v in data.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        candidates = v
                        break

    if not candidates and isinstance(payload, dict):
        try:
            preview = {k: type(v).__name__ for k, v in list(payload.items())[:8]}
            print(f"[debug] API 结构无法识别: {preview}")
        except Exception:
            pass
        return []

    out = []
    for it in candidates:
        if not isinstance(it, dict):
            continue
        lower = {str(k).lower(): v for k, v in it.items()}

        def g(*names):
            for n in names:
                if n.lower() in lower and lower[n.lower()] is not None:
                    return lower[n.lower()]
            return None

        # smis 字段：id / cnName / extremeRatio / platform / priceRatio7 / steamTransactionQuantity
        cid = g("id", "commodityId", "commodity_id", "goodsId", "goodId")
        name = g("cnName", "name", "commodityName", "goodsName", "hashName", "market_hash_name")
        if not name:
            continue

        platform = str(g("platform", "platformName") or "").upper()

        # 挂刀比例：优先 extremeRatio，再按平台取 *ToSteamBySellRatio
        ratio = g("extremeRatio", "ratio", "exchangeRatio")
        if ratio is None and platform:
            plat_key = {
                "BUFF": "buffToSteamBySellRatio",
                "UUYP": "uuypToSteamBySellRatio",
                "C5": "c5ToSteamBySellRatio",
                "IGXE": "igxeToSteamBySellRatio",
                "ECO": "ecoToSteamBySellRatio",
            }.get(platform)
            if plat_key:
                ratio = g(plat_key)

        ratio = normalize_ratio(ratio)
        if ratio is None:
            continue

        change_7d = g("priceRatio7", "change7d", "change_7d", "weekChange", "rise")
        if isinstance(change_7d, (int, float)):
            # smis 的 priceRatio7 可能是涨跌比例（如 -0.0609 或 -6.09）
            change_7d = float(change_7d)
            if abs(change_7d) <= 1.5:
                change_7d = change_7d * 100
        else:
            change_7d = parse_percent(change_7d)

        volume = g("steamTransactionQuantity", "volume", "turnover", "dayVolume", "sellNum")
        if not isinstance(volume, (int, float)):
            volume = parse_number(volume)

        steam_price = g("steamSellPrice", "steamPrice", "steam_price")
        if not isinstance(steam_price, (int, float)):
            steam_price = parse_number(steam_price)

        # 平台售价：按 platform 取对应 sellPrice
        platform_price = None
        if platform:
            pk = {
                "BUFF": "buffSellPrice",
                "UUYP": "uuypSellPrice",
                "C5": "c5SellPrice",
                "IGXE": "igxeSellPrice",
                "ECO": "ecoSellPrice",
            }.get(platform)
            if pk:
                platform_price = g(pk)
        if platform_price is None:
            platform_price = g("platformPrice", "platform_price", "sellPrice")
        if not isinstance(platform_price, (int, float)):
            platform_price = parse_number(platform_price)

        # 到手 Steam 余额约 = 平台价 / 比例
        steam_balance = g("steamBalance", "steam_balance", "toSteam")
        if steam_balance is None and platform_price and ratio and ratio > 0:
            try:
                steam_balance = float(platform_price) / float(ratio)
            except Exception:
                steam_balance = None
        if not isinstance(steam_balance, (int, float)):
            steam_balance = parse_number(steam_balance)

        out.append({
            "name": str(name).strip(),
            "commodity_id": cid,
            "change_7d": change_7d,
            "volume": volume,
            "steam_price": steam_price,
            "platform_price": platform_price,
            "steam_balance": steam_balance,
            "ratio": ratio,
            "platform": platform,
            "steam_url": "https://steamcommunity.com/market/search?appid=730&q=" + quote(str(name)),
            "smis_url": f"https://smis.club/commodity/{cid}" if cid is not None else None,
            "updated": str(g("updateTime", "updated") or ""),
            "scraped_at": now_utc().isoformat(),
        })
    return out



def parse_rows(raw_rows):
    out = []
    for row in raw_rows:
        headers = row.get("headers") or []
        cells = row.get("cells") or []
        if len(cells) < 4:
            continue

        data = {}
        for i in range(min(len(headers), len(cells))):
            h = (headers[i] or "").strip()
            if h:
                data[h] = cells[i]

        def col(*keywords):
            for h, v in data.items():
                if any(k in h for k in keywords):
                    return v
            return None

        name = col("饰品名称", "商品", "名称")
        if not name:
            for c in cells:
                t = re.sub(r"\s+", " ", str(c)).strip()
                if len(t) >= 2 and not re.fullmatch(r"[\d.%+\-¥￥,\s]+", t) and t not in {"刚刚", "Steam"}:
                    name = t
                    break
        if name:
            name = re.sub(r"\s+", " ", name).strip()

        change_7d = parse_percent(col("七日涨跌", "7日涨跌", "涨跌"))
        volume = parse_number(col("成交量"))
        steam_price = parse_number(col("Steam售价", "Steam售"))
        platform_price = parse_number(col("平台售价", "平台价"))
        steam_balance = parse_number(col("到手Steam余额", "Steam余额", "到手余额"))
        ratio = normalize_ratio(col("挂刀比例", "比例"))
        platform = col("交易平台") or ""
        market_link = col("Steam市场") or ""
        updated = col("更新时间") or ""

        if ratio is None:
            for c in cells:
                m = re.search(r"\b0\.\d{2,4}\b", str(c))
                if m:
                    ratio = float(m.group())
                    break
        if change_7d is None:
            for c in cells:
                m = re.search(r"([+\-]?\d+(?:\.\d+)?)\s*%", str(c))
                if m:
                    change_7d = float(m.group(1))
                    break

        if not name or ratio is None:
            continue

        steam_url = (
            market_link
            if str(market_link).startswith("http")
            else "https://steamcommunity.com/market/search?appid=730&q=" + quote(name)
        )

        out.append(
            {
                "name": name,
                "change_7d": change_7d,
                "volume": volume,
                "steam_price": steam_price,
                "platform_price": platform_price,
                "steam_balance": steam_balance,
                "ratio": ratio,
                "platform": platform,
                "steam_url": steam_url,
                "updated": updated,
                "scraped_at": now_utc().isoformat(),
            }
        )
    return out


def scrape_exchange(config):
    """低配 VPS 优化：仍完整走「填筛选 → 应用设置 → 用应用后的接口数据」。"""
    url = config.get("site", {}).get("url", "https://smis.club/exchange")
    wait_ms = int(config.get("site", {}).get("wait_ms", 8000))
    filters = config.get("filters", {})

    price_min = filters.get("price_min", 1)
    price_max = filters.get("price_max", 5000)
    page_volume = max(10, min(int(filters.get("volume_min", 50)), 50))

    setup_mode = os.getenv("SMIS_SETUP", "").strip() in {"1", "true", "True", "yes", "YES"}
    headed_env = os.getenv("SMIS_HEADED", "").strip() in {"1", "true", "True", "yes", "YES"}
    headed = setup_mode or headed_env
    manual_wait = int(os.getenv("SMIS_MANUAL_WAIT", "0") or "0")
    if setup_mode and manual_wait <= 0:
        manual_wait = 90

    # SMIS_PERSISTENT=1 时用用户目录（本机过验证）；VPS 默认不用，省内存
    use_persistent = os.getenv("SMIS_PERSISTENT", "").strip() in {"1", "true", "True", "yes", "YES"} or setup_mode

    chrome_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-translate",
        "--mute-audio",
        "--no-first-run",
        "--no-zygote",
        "--renderer-process-limit=1",
        "--js-flags=--max-old-space-size=192",
    ]
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
    viewport = {"width": 900, "height": 700}

    max_attempts = 2
    last_err = None
    warnings = []

    for attempt in range(1, max_attempts + 1):
        api_payloads = []
        post_apply_payloads = []
        apply_clicked = False
        context = None
        browser = None

        try:
            with sync_playwright() as p:
                if use_persistent:
                    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=str(BROWSER_DIR),
                        headless=not headed,
                        viewport=viewport,
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                        args=chrome_args,
                        user_agent=ua,
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                else:
                    browser = p.chromium.launch(headless=not headed, args=chrome_args)
                    context = browser.new_context(
                        viewport=viewport,
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                        user_agent=ua,
                    )
                    page = context.new_page()

                def on_response(resp):
                    try:
                        u = resp.url or ""
                        if "commodity/exchange" not in u and not (
                            "exchange" in u and "/api/" in u
                        ):
                            return
                        if resp.status != 200:
                            print(f"[warn] 接口状态 {resp.status}: {u[:90]}")
                            return
                        data = resp.json()
                        api_payloads.append(data)
                        if apply_clicked:
                            post_apply_payloads.append(data)
                        keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                        tag = "应用后" if apply_clicked else "应用前"
                        print(f"[info] 拦截接口({tag}) {resp.status}: {u[:80]} keys={keys}")
                    except Exception:
                        pass

                page.on("response", on_response)

                print(
                    f"[info] 打开 {url} (attempt={attempt}/{max_attempts}, "
                    f"headed={headed}, persistent={use_persistent})"
                )
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)

                if manual_wait > 0:
                    print(f"[info] 手动验证等待最多 {manual_wait}s...")
                    for _w in range(manual_wait):
                        page.wait_for_timeout(1000)
                        if any(items_from_api_payload(pl) for pl in api_payloads):
                            print(f"[info] 已有接口数据，提前继续 ({_w+1}s)")
                            break

                # ---------- 填筛选 ----------
                print(f"[info] 设置筛选: 价格 {price_min}~{price_max}, 成交量>={page_volume}")

                def fill_near_label(label_text, values):
                    loc = page.get_by_text(label_text, exact=False).first
                    row = loc.locator(
                        "xpath=ancestor::div[contains(@class,'el-') or contains(@class,'form') or contains(@class,'item')][1]"
                    )
                    if row.count() == 0:
                        row = loc.locator("xpath=ancestor::div[2]")
                    inputs = row.locator(
                        "input:not([type='radio']):not([type='checkbox']):not([type='hidden'])"
                    )
                    n = inputs.count()
                    print(f"[info] 标签「{label_text}」附近 input 数量: {n}")
                    for i, v in enumerate(values):
                        if i >= n:
                            break
                        el = inputs.nth(i)
                        el.click(timeout=3000)
                        el.fill("")
                        el.fill(str(v))
                        el.press("Tab")
                    return n

                try:
                    try:
                        rst = page.locator("button:has-text('重置设置')")
                        if rst.count() > 0:
                            rst.first.click(timeout=2000)
                            page.wait_for_timeout(600)
                            print("[info] 已点重置设置")
                    except Exception:
                        pass

                    n_price = fill_near_label("价格区间", [price_min, price_max])
                    if n_price < 2:
                        print("[warn] 价格区间定位失败，尝试 el-input")
                        all_in = page.locator("input.el-input__inner")
                        vis = []
                        for i in range(min(all_in.count(), 12)):
                            try:
                                if all_in.nth(i).is_visible():
                                    vis.append(i)
                            except Exception:
                                pass
                        if len(vis) >= 2:
                            all_in.nth(vis[0]).fill(str(price_min))
                            all_in.nth(vis[1]).fill(str(price_max))
                            if len(vis) >= 3:
                                all_in.nth(vis[2]).fill(str(page_volume))
                    else:
                        fill_near_label("日成交量", [page_volume])

                    try:
                        shown = page.evaluate(
                            """() => {
                            const pick = (label) => {
                              const nodes = Array.from(document.querySelectorAll('div,span,label'));
                              for (const n of nodes) {
                                const t = (n.textContent || '').trim();
                                if (t.startsWith(label) || t.includes(label)) {
                                  let p = n;
                                  for (let i=0;i<5 && p;i++, p=p.parentElement) {
                                    const ins = Array.from(p.querySelectorAll('input')).filter(el => {
                                      const ty=(el.type||'').toLowerCase();
                                      return !['radio','checkbox','hidden'].includes(ty);
                                    });
                                    if (ins.length) return ins.map(el => el.value);
                                  }
                                }
                              }
                              return [];
                            };
                            return { price: pick('价格'), volume: pick('日成交') };
                            }"""
                        )
                        print(f"[info] 读回输入框: {shown}")
                    except Exception as e:
                        print(f"[warn] 读回失败: {e}")
                except Exception as e:
                    print(f"[warn] 设置筛选失败: {e}")
                    raise

                page.wait_for_timeout(400)

                # ---------- 应用设置（之后的接口才是筛选后结果）----------
                clicked = False
                for text in ["应用设置", "应用"]:
                    btn = page.locator(f"button:has-text('{text}')")
                    if btn.count() > 0:
                        try:
                            # 清空「应用前」干扰：只认应用后的包
                            post_apply_payloads.clear()
                            apply_clicked = True
                            btn.first.click(timeout=3000)
                            print(f"[info] 已点击「{text}」")
                            clicked = True
                            break
                        except Exception as e:
                            print(f"[warn] 点击 {text} 失败: {e}")
                if not clicked:
                    print("[warn] 未找到应用按钮，仍等待接口...")
                    apply_clicked = True

                # ---------- 等待「应用后」接口 ----------
                print("[info] 等待筛选后的接口数据...")
                got = []
                page_crashed = False
                for i in range(25):
                    try:
                        page.wait_for_timeout(800)
                    except Exception as e:
                        page_crashed = True
                        msg = f"等待中页面异常: {e}"
                        print(f"[warn] {msg}")
                        warnings.append(msg)
                        break
                    if post_apply_payloads:
                        for pl in post_apply_payloads:
                            part = items_from_api_payload(pl)
                            if part:
                                got = part
                                break
                        if got:
                            print(f"[info] 应用后接口解析到 {len(got)} 条 (第 {i+1} 次等待)")
                            break
                    if i in (8, 16) and not post_apply_payloads:
                        try:
                            page.locator("button:has-text('应用设置')").first.click(timeout=1500)
                            print("[info] 再次点击应用设置")
                        except Exception as e:
                            warnings.append(f"再次点击应用失败: {e}")

                used_fallback = False
                if not got:
                    for pl in reversed(api_payloads):
                        part = items_from_api_payload(pl)
                        if part:
                            got = part
                            used_fallback = True
                            msg = (
                                f"未捕获到应用后接口，暂用已有数据 {len(got)} 条"
                                "（可能未重新请求）"
                            )
                            print(f"[warn] {msg}")
                            warnings.append(msg)
                            break

                try:
                    context.close()
                except Exception:
                    pass
                try:
                    if browser:
                        browser.close()
                except Exception:
                    pass

                # 页面崩溃且没有应用后数据：优先重试，而不是直接用脏数据返回
                if (page_crashed or used_fallback) and not post_apply_payloads:
                    if attempt < max_attempts:
                        raise RuntimeError(
                            "页面崩溃或未拿到筛选后接口，准备重试"
                            + (f"（已有兜底 {len(got)} 条）" if got else "")
                        )
                    # 最后一轮才接受兜底
                    if got:
                        warnings.append(f"第 {attempt} 次仍异常，使用兜底数据 {len(got)} 条")
                        return got, warnings

                if got:
                    return got, warnings

                print("[warn] 接口无有效列表，尝试结束本轮")
                last_err = RuntimeError("无有效商品数据")
        except Exception as e:
            last_err = e
            msg = f"第 {attempt} 次尝试失败: {e}"
            print(f"[error] {msg}")
            warnings.append(msg)
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            if attempt < max_attempts:
                print("[info] 2s 后重试...")
                time.sleep(2)

    if last_err:
        # 若有过兜底机会已在上面 return；这里彻底失败
        warnings.append(str(last_err))
        return [], warnings
    return [], warnings



def qualify(item, filters):
    ratio_max = float(filters.get("ratio_max", 0.70))
    drop_min = float(filters.get("drop_min_pct", 3))
    volume_min = float(filters.get("volume_min", 50))
    price_min = filters.get("price_min")
    price_max = filters.get("price_max")
    platforms = [str(x).lower() for x in filters.get("platforms", []) if str(x).strip()]

    if item["ratio"] is None or item["ratio"] > ratio_max:
        return False
    if item["change_7d"] is None or item["change_7d"] > -drop_min:
        return False
    if item["volume"] is not None and item["volume"] < volume_min:
        return False
    if platforms and item["platform"].lower() not in platforms:
        return False
    p = item["platform_price"]
    if price_min is not None and (p is None or p < float(price_min)):
        return False
    if price_max is not None and (p is None or p > float(price_max)):
        return False
    return True


def key_for(item):
    return f"{item['name']}::{item['platform']}"


def fmt_money(v):
    return "—" if v is None else f"¥{v:,.2f}"


def fmt_pct(v):
    return "—" if v is None else f"{v:+.2f}%"



def smis_item_url(item):
    """详情页 https://smis.club/commodity/{id}"""
    if item.get("smis_url"):
        return item["smis_url"]
    cid = item.get("commodity_id") or item.get("id")
    if cid is not None and str(cid).strip() != "":
        return f"https://smis.club/commodity/{cid}"
    # 无 id 时退回挂刀列表页
    return "https://smis.club/exchange"



def telegram_send(bot_token, chat_id, text):
    """发送消息，返回 message_id。"""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API: {data}")
    return data.get("result", {}).get("message_id")


def telegram_get_updates(bot_token, offset=None, timeout=0):
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(url, params=params, timeout=max(25, timeout + 5))
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates: {data}")
    return data.get("result") or []


def parse_buy_time_and_indices(text, msg_date_ts=None):
    """
    解析购买时间 + 序号。
    时间优先级：
      1) 消息正文里写的时间（写在「已买」前面）
      2) Telegram 消息时间 msg.date（用户点发送的时间）
    支持示例：
      已买1
      已买1,3
      15:30 已买1
      09-10 15:30 已买1
      2026-09-10 15:30 已买1
      2026/9/10 15:30 已买1,2
    返回 (bought_at: datetime UTC, indices: list[int], time_source: str)
    """
    if not text:
        return None, [], ""
    raw = re.sub(r"@\w+", "", text.strip()).strip()
    if not re.search(r"已买|买入|买了|(?:^|\s)买(?:\s|$|\d)", raw):
        return None, [], ""

    # 上海时区解释「本地时间」
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Shanghai")
    except Exception:
        tz = timezone(timedelta(hours=8))

    bought_at = None
    time_source = ""
    work = raw

    # 完整日期时间
    patterns = [
        (r"(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})[\sT]+(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?", "manual"),
        (r"(?P<m>\d{1,2})[-/](?P<d>\d{1,2})[\s]+(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?", "manual_md"),
        (r"(?<![\d])(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?(?=\s*已买|\s*买)", "manual_hm"),
    ]
    for pat, kind in patterns:
        m = re.search(pat, work)
        if not m:
            continue
        gd = m.groupdict()
        now_local = datetime.now(tz)
        try:
            y = int(gd["y"]) if gd.get("y") else now_local.year
            if kind == "manual_md":
                mo, d = int(gd["m"]), int(gd["d"])
            elif kind == "manual_hm":
                mo, d = now_local.month, now_local.day
            else:
                mo, d = int(gd["m"]), int(gd["d"])
            H = int(gd["H"])
            M = int(gd["M"])
            S = int(gd["S"] or 0)
            local_dt = datetime(y, mo, d, H, M, S, tzinfo=tz)
            # 仅写时刻且比「现在」晚很多（跨日）：若大于当前 6 小时，视为昨天
            if kind == "manual_hm" and local_dt > now_local + timedelta(hours=6):
                local_dt = local_dt - timedelta(days=1)
            bought_at = local_dt.astimezone(UTC)
            time_source = "text"
            # 从文本中去掉这段时间，避免把年/月/日当成序号
            work = work[: m.start()] + " " + work[m.end() :]
            break
        except Exception:
            continue

    if bought_at is None and msg_date_ts is not None:
        try:
            bought_at = datetime.fromtimestamp(int(msg_date_ts), tz=UTC)
            time_source = "tg_message"
        except Exception:
            bought_at = now_utc()
            time_source = "now"
    if bought_at is None:
        bought_at = now_utc()
        time_source = "now"

    # 序号：只取「买」后面的数字，避免日期残渣
    tail = work
    m_buy = re.search(r"(已买|买入|买了|(?:^|\s)买)\s*(.*)$", work)
    if m_buy:
        tail = m_buy.group(2) or ""
    nums = re.findall(r"\d+", tail)
    # 若尾部没有数字，再在全文买字后找
    if not nums:
        nums = re.findall(r"(?:已买|买入|买了|买)\s*(\d+)", work)
    indices = []
    seen = set()
    for n in nums:
        try:
            i = int(n)
        except Exception:
            continue
        if 1 <= i <= 50 and i not in seen:
            seen.add(i)
            indices.append(i)
    return bought_at, indices, time_source


def process_buy_replies(token, chat_id, state, hold_days):
    """处理「已买N」：买入时间优先正文，其次 TG 发送时间。
    同一条消息若被编辑，只按最终内容处理一次，避免重复确认。
    """
    offset = state.get("tg_update_offset")
    try:
        updates = telegram_get_updates(token, offset=offset)
    except Exception as e:
        print(f"[warn] 拉取 TG 消息失败: {e}")
        return 0

    last_batch = state.get("last_batch") or []
    batch_by_msg = state.get("batch_by_msg") or {}
    candidates = state.setdefault("candidates", {})
    marked = 0
    max_update_id = None

    # 同一 message_id 多次编辑：只保留最新 update
    latest_by_msg = {}
    for upd in updates:
        uid = upd.get("update_id")
        if uid is not None:
            max_update_id = uid if max_update_id is None else max(max_update_id, uid)

        msg = upd.get("edited_message") or upd.get("message")
        if not msg:
            continue
        chat = msg.get("chat") or {}
        if str(chat.get("id")) != str(chat_id):
            continue
        mid = msg.get("message_id")
        if mid is None:
            continue
        prev = latest_by_msg.get(mid)
        if prev is None or (uid is not None and uid >= prev[0]):
            latest_by_msg[mid] = (uid if uid is not None else -1, msg)

    for mid, (_uid, msg) in latest_by_msg.items():
        text_body = msg.get("text") or ""
        ts = msg.get("edit_date") or msg.get("date")
        bought_at, indices, time_source = parse_buy_time_and_indices(
            text_body, msg_date_ts=ts
        )
        if not indices:
            continue

        batch = last_batch
        reply = msg.get("reply_to_message") or {}
        reply_id = reply.get("message_id")
        if reply_id is not None and str(reply_id) in batch_by_msg:
            batch = batch_by_msg[str(reply_id)]

        if not batch:
            try:
                telegram_send(
                    token,
                    chat_id,
                    "⚠️ 暂无榜单可标记。等推送 Top 后回复：" + chr(10) + "<code>已买1</code>" + chr(10) + "或带时间：<code>2026-09-10 15:30 已买1</code>",
                )
            except Exception:
                pass
            continue

        unlock = bought_at + timedelta(days=hold_days)
        names = []
        for idx in indices:
            if idx < 1 or idx > len(batch):
                continue
            entry = batch[idx - 1]
            key = entry.get("key")
            if not key:
                continue
            rec = candidates.get(key) or {}
            rec.update(
                {
                    "name": entry.get("name") or rec.get("name"),
                    "platform": entry.get("platform") or rec.get("platform"),
                    "smis_url": entry.get("smis_url") or rec.get("smis_url"),
                    "commodity_id": entry.get("commodity_id") or rec.get("commodity_id"),
                    "steam_url": entry.get("steam_url") or rec.get("steam_url"),
                    "status": "bought",
                    "bought_at": bought_at.isoformat(),
                    "unlock_at": unlock.isoformat(),
                    "buy_time_source": time_source,
                    "buy_index": idx,
                    "buy_from_msg_id": mid,
                }
            )
            candidates[key] = rec
            names.append(f"{idx}. {rec.get('name')}")
            marked += 1
            print(
                f"[tg] 已标记购买: {rec.get('name')}  "
                f"bought={bought_at.isoformat()} ({time_source})  "
                f"unlock={unlock.isoformat()}"
            )

        if not names:
            continue

        try:
            from zoneinfo import ZoneInfo

            local = bought_at.astimezone(ZoneInfo("Asia/Shanghai"))
            unlock_s = unlock.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
                "%Y-%m-%d %H:%M"
            )
        except Exception:
            local = bought_at + timedelta(hours=8)
            unlock_s = (
                bought_at + timedelta(days=hold_days) + timedelta(hours=8)
            ).strftime("%Y-%m-%d %H:%M")
        local_s = local.strftime("%Y-%m-%d %H:%M")
        src = {
            "text": "消息内时间",
            "tg_message": "TG发送时间",
            "now": "脚本处理时间",
        }.get(time_source, time_source)
        body = chr(10).join(names)
        try:
            telegram_send(
                token,
                chat_id,
                (
                    f"✅ 已记录购买（{src} <b>{local_s}</b>）"
                    + chr(10)
                    + f"约 <b>{hold_days:g}</b> 天后提醒（{unlock_s}）："
                    + chr(10)
                    + body
                ),
            )
        except Exception as e:
            print(f"[warn] 确认消息失败: {e}")

    if max_update_id is not None:
        state["tg_update_offset"] = max_update_id + 1
    return marked



def discovery_batch_message(items, sale_method, hold_days):
    """最多 10 条合并成一条 TG 消息。"""
    lines = [
        f"🟢 <b>挂刀机会 Top {len(items)}</b>",
        f"策略: {sale_method} · 保护期约 {hold_days:g} 天",
        "━━━━━━━━━━━━━━━━",
    ]
    for i, item in enumerate(items, 1):
        url = smis_item_url(item)
        name = item.get("name") or "?"
        ratio = item.get("ratio")
        ratio_s = f"{ratio:.4f}" if ratio is not None else "—"
        ch_s = fmt_pct(item.get("change_7d"))
        vol = item.get("volume")
        vol_s = f"{vol:g}" if isinstance(vol, (int, float)) else "—"
        plat = item.get("platform") or "—"
        pp = fmt_money(item.get("platform_price"))
        sb = fmt_money(item.get("steam_balance"))
        lines.append(
            f"<b>{i}. <a href=\"{url}\">{name}</a></b>\n"
            f"   比例 <b>{ratio_s}</b> · 7日 <b>{ch_s}</b> · {plat}\n"
            f"   平台价 {pp} → 到手 {sb} · 量 {vol_s}"
        )
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("买了请回复 Bot（不要只在群里顺口说）：")
    lines.append("<code>已买1</code> 或 <code>已买1,3</code>（数字=上方序号）")
    lines.append("可写时间：<code>09-10 15:30 已买1</code> 或 <code>15:30 已买1</code>")
    lines.append("未写时间则用你在 TG 点发送的时间起算保护期。")
    lines.append('<a href="https://smis.club/exchange">打开挂刀行情</a>')
    return "\n".join(lines)


def mature_message(item, record, sale_method):
    ratio = item.get("ratio")
    ratio_text = f"{ratio:.4f}" if ratio is not None else "—"
    url = smis_item_url(item if item.get("name") else record)
    name = item.get("name", record.get("name"))
    bought = record.get("bought_at") or "—"
    return (
        "⏰ <b>购买保护期到期 · 可考虑上架</b>\n\n"
        f"<b><a href=\"{url}\">{name}</a></b>\n"
        f"平台：{item.get('platform', record.get('platform', '—'))}\n"
        f"标记购买：{bought}\n"
        f"当前挂刀比例：<b>{ratio_text}</b>\n"
        f"当前7日涨跌：<b>{fmt_pct(item.get('change_7d'))}</b>\n"
        f"平台价：{fmt_money(item.get('platform_price'))}\n"
        f"Steam售价：{fmt_money(item.get('steam_price'))}\n"
        f"到手余额：{fmt_money(item.get('steam_balance'))}\n\n"
        f"建议：按 <b>{sale_method}</b> 核对后出售。\n"
        f'<a href="https://smis.club/exchange">挂刀行情</a>'
    )


def run():
    config = get_config()
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("缺少环境变量 TG_BOT_TOKEN 或 TG_CHAT_ID")

    state = load_json(DATA_FILE, {"candidates": {}})
    candidates = state.setdefault("candidates", {})
    filters = config.get("filters", {})
    sale_method = config.get("strategy", {}).get("sale_method", "Steam挂底价")
    hold_days = float(config.get("strategy", {}).get("hold_days", 7))

    # 先处理 TG「已买」回复（不依赖本轮抓取）
    try:
        n_buy = process_buy_replies(token, chat_id, state, hold_days)
        if n_buy:
            print(f"[info] 本轮标记购买 {n_buy} 件")
            save_json(DATA_FILE, state)
    except Exception as e:
        print(f"[warn] 处理已买回复异常: {e}")

    print("[info] 开始抓取...")
    scrape_warnings = []
    try:
        scraped = scrape_exchange(config)
        if isinstance(scraped, tuple):
            rows, scrape_warnings = scraped
        else:
            rows = scraped
    except Exception as e:
        scrape_warnings.append(str(e))
        raise
    print(f"[info] 解析到 {len(rows)} 条有效商品")

    for i, r in enumerate(rows[:8]):
        print(
            f"  [{i+1}] {r['name'][:24]:24s}  ratio={r['ratio']:.4f}  "
            f"7d={r['change_7d'] if r['change_7d'] is None else round(r['change_7d'], 2)}  "
            f"vol={r['volume']}  plat={r['platform']}"
        )

    by_key = {key_for(x): x for x in rows}
    print(
        f"[info] 过滤条件: ratio<={filters.get('ratio_max')} "
        f"跌幅>={filters.get('drop_min_pct')}% "
        f"vol>={filters.get('volume_min')} "
        f"价 {filters.get('price_min')}~{filters.get('price_max')}"
    )
    qualified = []
    skip_shown = 0
    for x in rows:
        if qualify(x, filters):
            qualified.append(x)
        elif skip_shown < 5:
            reasons = []
            rm = float(filters.get("ratio_max", 0.70))
            dm = float(filters.get("drop_min_pct", 3))
            vm = float(filters.get("volume_min", 50))
            if x["ratio"] is None or x["ratio"] > rm:
                reasons.append(f"ratio={x['ratio']}>{rm}")
            if x["change_7d"] is None or x["change_7d"] > -dm:
                reasons.append(f"7d={x['change_7d']}未跌够{dm}%")
            if x["volume"] is not None and x["volume"] < vm:
                reasons.append(f"vol={x['volume']}<{vm}")
            print(f"  [skip] {str(x['name'])[:18]}: {', '.join(reasons) or '其他'}")
            skip_shown += 1
    qualified.sort(key=lambda x: (x["ratio"], -(x["volume"] or 0)))

    print(f"[info] 符合过滤条件: {len(qualified)} 条")
    for q in qualified[:10]:
        print(f"  ✓ {q['name']}  ratio={q['ratio']:.4f}  7d={q['change_7d']}%")

    changed = False
    new_count = 0

    # 仅登记发现，不自动开始 7 天（等用户「已买」）
    for item in qualified:
        key = key_for(item)
        if key not in candidates:
            candidates[key] = {
                "name": item["name"],
                "platform": item["platform"],
                "steam_url": item.get("steam_url"),
                "smis_url": smis_item_url(item),
                "commodity_id": item.get("commodity_id"),
                "first_seen": now_utc().isoformat(),
                "status": "seen",
            }
            new_count += 1
            changed = True
        else:
            # 更新展示信息，但不要覆盖 bought
            rec = candidates[key]
            if rec.get("status") not in ("bought", "matured"):
                rec["smis_url"] = smis_item_url(item)
                rec["commodity_id"] = item.get("commodity_id") or rec.get("commodity_id")

    batch = qualified[:10]
    if batch:
        try:
            body = discovery_batch_message(batch, sale_method, hold_days)
            if scrape_warnings:
                body += chr(10) + "━━━━━━━━━━━━━━━━" + chr(10) + "<b>本轮警告：</b>" + chr(10)
                body += chr(10).join("• " + str(w)[:100] for w in scrape_warnings[:5])
            msg_id = telegram_send(token, chat_id, body)
            batch_entries = []
            for it in batch:
                batch_entries.append(
                    {
                        "key": key_for(it),
                        "name": it.get("name"),
                        "platform": it.get("platform"),
                        "smis_url": smis_item_url(it),
                        "commodity_id": it.get("commodity_id"),
                        "steam_url": it.get("steam_url"),
                    }
                )
            state["last_batch"] = batch_entries
            if msg_id is not None:
                bmap = state.setdefault("batch_by_msg", {})
                bmap[str(msg_id)] = batch_entries
                # 只保留最近 20 条榜单消息
                if len(bmap) > 20:
                    for k in list(bmap.keys())[:-20]:
                        bmap.pop(k, None)
            changed = True
            print(f"[tg] 已推送 Top {len(batch)}（本轮新登记 {new_count}）msg_id={msg_id}")
            for it in batch:
                print(f"  → id={it.get('commodity_id')} {it['name']}")
        except Exception as e:
            print(f"[error] Telegram 发送失败: {e}")
    else:
        print("[info] 无符合条件商品，推送本轮说明")
        try:
            lines = [
                "⚪ <b>本轮无符合条件饰品</b>",
                f"抓取 {len(rows)} 条 · 合格 0 条",
                f"过滤: ratio≤{filters.get('ratio_max')} · 跌幅≥{filters.get('drop_min_pct')}% · vol≥{filters.get('volume_min')}",
            ]
            if scrape_warnings:
                lines.append("━━━━━━━━━━━━━━━━")
                lines.append("<b>执行异常/警告：</b>")
                for w in scrape_warnings[:8]:
                    lines.append("• " + str(w)[:120])
            telegram_send(token, chat_id, chr(10).join(lines))
        except Exception as e:
            print(f"[error] 空结果说明发送失败: {e}")

    # 仅对「已买」且到期的条目提醒
    for key, record in list(candidates.items()):
        if record.get("status") != "bought":
            continue
        try:
            unlock_at = datetime.fromisoformat(record["unlock_at"])
        except Exception:
            continue
        if now_utc() < unlock_at:
            continue
        item = by_key.get(key) or {
            **record,
            "ratio": None,
            "change_7d": None,
            "platform_price": None,
            "steam_price": None,
            "steam_balance": None,
            "volume": None,
        }
        try:
            telegram_send(token, chat_id, mature_message(item, record, sale_method))
            print(f"[tg] 已推送到期复核: {record.get('name')}")
        except Exception as e:
            print(f"[error] Telegram 发送失败: {e}")
        record["status"] = "matured"
        record["matured_at"] = now_utc().isoformat()
        changed = True

    max_records = int(config.get("state", {}).get("max_records", 3000))
    if len(candidates) > max_records:
        matured = [(k, v) for k, v in candidates.items() if v.get("status") == "matured"]
        matured.sort(key=lambda kv: kv[1].get("matured_at", ""))
        for k, _ in matured[: max(0, len(candidates) - max_records)]:
            candidates.pop(k, None)
            changed = True

    if changed:
        save_json(DATA_FILE, state)

    summary = {
        "scraped": len(rows),
        "qualified": len(qualified),
        "new_candidates": new_count,
        "bought_waiting": sum(1 for v in candidates.values() if v.get("status") == "bought"),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return summary



if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)
