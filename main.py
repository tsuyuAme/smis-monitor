import json, os, re, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / 'data' / 'state.json'
CONFIG_FILE = BASE_DIR / 'config.json'

UTC = timezone.utc


def now_utc():
    return datetime.now(UTC)


def parse_number(text):
    if text is None:
        return None
    s = str(text).strip().replace(',', '').replace('¥', '').replace('$', '')
    if not s or s in {'-', '--', 'N/A', '暂无'}:
        return None
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    return float(m.group()) if m else None


def parse_percent(text):
    if text is None:
        return None
    s = str(text).strip().replace('%', '')
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    return float(m.group()) if m else None


def normalize_ratio(text):
    if text is None:
        return None
    v = parse_number(text)
    if v is None:
        return None
    # Accept both 0.63 and 63% / 63 forms.
    return v / 100 if v > 1.5 else v


def load_json(path, default):
    try:
        return json.loads(path.read_text('utf-8'))
    except Exception:
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), 'utf-8')


def get_config():
    cfg = load_json(CONFIG_FILE, {})
    env_override = os.getenv('SMIS_CONFIG_JSON')
    if env_override:
        cfg.update(json.loads(env_override))
    return cfg


def headers_from_table(page):
    return page.locator('table thead tr').first.locator('th').all_inner_texts()


def scrape_exchange(config):
    url = config.get('site', {}).get('url', 'https://smis.club/exchange')
    wait_ms = int(config.get('site', {}).get('wait_ms', 5000))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1200}, locale='zh-CN')
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        try:
            page.wait_for_load_state('networkidle', timeout=20000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(wait_ms)

        tables = page.locator('table')
        if tables.count() == 0:
            # Helpful diagnostic HTML if site markup changes.
            html = page.content()
            browser.close()
            raise RuntimeError('未找到排行榜 table。smis.club 页面结构可能发生变化。')

        # Use the first non-empty table with a header containing 商品/饰品/七日涨跌/挂刀比例 etc.
        chosen = None
        chosen_headers = []
        for i in range(tables.count()):
            t = tables.nth(i)
            hs = t.locator('thead tr').first.locator('th').all_inner_texts()
            joined = ' '.join(hs)
            if any(k in joined for k in ['挂刀比例', '七日涨跌', 'Steam售价', '饰品名称']):
                chosen = t
                chosen_headers = [h.strip() for h in hs]
                break
        if chosen is None:
            browser.close()
            raise RuntimeError('找到 table，但没有识别出 exchange 排行榜表头。')

        rows = chosen.locator('tbody tr')
        results = []
        for i in range(rows.count()):
            cells = rows.nth(i).locator('td').all_inner_texts()
            if not cells:
                continue
            results.append({'headers': chosen_headers, 'cells': [c.strip() for c in cells]})

        browser.close()

    return parse_rows(results)


def parse_rows(raw_rows):
    # Known smis column order from the current exchange page.
    # We also tolerate minor header changes by keyword matching.
    out = []
    for row in raw_rows:
        headers = row['headers']
        cells = row['cells']
        if len(cells) < 5:
            continue
        data = {headers[i]: cells[i] for i in range(min(len(headers), len(cells)))}

        def col(*keywords):
            for h, v in data.items():
                if any(k in h for k in keywords):
                    return v
            return None

        name = col('饰品名称', '商品', '名称') or cells[0]
        change_7d = parse_percent(col('七日涨跌', '7日涨跌', '涨跌'))
        volume = parse_number(col('成交量'))
        steam_price = parse_number(col('Steam售价'))
        platform_price = parse_number(col('平台售价'))
        steam_balance = parse_number(col('到手Steam余额'))
        ratio = normalize_ratio(col('挂刀比例'))
        platform = col('交易平台') or ''
        market_link = col('Steam市场') or ''
        updated = col('更新时间') or ''

        if name and ratio is not None:
            # Build a best-effort Steam Community Market search URL.
            steam_url = market_link if market_link.startswith('http') else (
                'https://steamcommunity.com/market/search?appid=730&q=' + quote(name)
            )
            out.append({
                'name': name,
                'change_7d': change_7d,
                'volume': volume,
                'steam_price': steam_price,
                'platform_price': platform_price,
                'steam_balance': steam_balance,
                'ratio': ratio,
                'platform': platform,
                'steam_url': steam_url,
                'updated': updated,
                'scraped_at': now_utc().isoformat(),
            })
    return out


def qualify(item, filters):
    ratio_max = float(filters.get('ratio_max', 0.65))
    drop_min = float(filters.get('drop_min_pct', 5))
    volume_min = float(filters.get('volume_min', 0))
    price_min = filters.get('price_min')
    price_max = filters.get('price_max')
    platforms = [str(x).lower() for x in filters.get('platforms', []) if str(x).strip()]

    if item['ratio'] is None or item['ratio'] > ratio_max:
        return False
    if item['change_7d'] is None or item['change_7d'] > -drop_min:
        return False
    if item['volume'] is not None and item['volume'] < volume_min:
        return False
    if platforms and item['platform'].lower() not in platforms:
        return False
    p = item['platform_price']
    if price_min is not None and (p is None or p < float(price_min)):
        return False
    if price_max is not None and (p is None or p > float(price_max)):
        return False
    return True


def key_for(item):
    return f"{item['name']}::{item['platform']}"


def fmt_money(v):
    return '—' if v is None else f'¥{v:,.2f}'


def fmt_pct(v):
    return '—' if v is None else f'{v:+.2f}%'


def telegram_send(bot_token, chat_id, text):
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    r = requests.post(url, json={
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }, timeout=20)
    r.raise_for_status()


def discovery_message(item, unlock_at, sale_method):
    return (
        '🟢 <b>发现挂刀机会</b>\n\n'
        f"<b>{item['name']}</b>\n"
        f"7日跌幅：<b>{fmt_pct(item['change_7d'])}</b>\n"
        f"挂刀比例：<b>{item['ratio']:.3f}</b>\n"
        f"平台：{item['platform'] or '—'}\n"
        f"平台价：{fmt_money(item['platform_price'])}\n"
        f"Steam售价：{fmt_money(item['steam_price'])}\n"
        f"到手余额：{fmt_money(item['steam_balance'])}\n"
        f"成交量：{item['volume'] if item['volume'] is not None else '—'}\n\n"
        f"策略：<b>{sale_method}</b>\n"
        f"预计可卖时间：<b>{unlock_at.astimezone().strftime('%Y-%m-%d %H:%M')}</b>\n"
        f"<a href=\"{item['steam_url']}\">打开 Steam 市场</a>"
    )


def mature_message(item, record, sale_method):
    ratio = item.get('ratio')
    ratio_text = f'{ratio:.3f}' if ratio is not None else '—'
    change = fmt_pct(item.get('change_7d'))
    return (
        '⏰ <b>7天保护期到期复核</b>\n\n'
        f"<b>{item.get('name', record['name'])}</b>\n"
        f"当前平台：{item.get('platform', record.get('platform','—'))}\n"
        f"当前挂刀比例：<b>{ratio_text}</b>\n"
        f"当前7日涨跌：<b>{change}</b>\n"
        f"平台价：{fmt_money(item.get('platform_price'))}\n"
        f"Steam售价：{fmt_money(item.get('steam_price'))}\n"
        f"到手余额：{fmt_money(item.get('steam_balance'))}\n\n"
        f"建议：按 <b>{sale_method}</b> 路径核对实时市场后出售。\n"
        f"<a href=\"{item.get('steam_url', record.get('steam_url',''))}\">打开 Steam 市场</a>"
    )


def run():
    config = get_config()
    token = os.getenv('TG_BOT_TOKEN')
    chat_id = os.getenv('TG_CHAT_ID')
    if not token or not chat_id:
        raise RuntimeError('缺少 TG_BOT_TOKEN / TG_CHAT_ID 环境变量')

    state = load_json(DATA_FILE, {'candidates': {}})
    candidates = state.setdefault('candidates', {})
    filters = config.get('filters', {})
    sale_method = config.get('strategy', {}).get('sale_method', 'Steam挂底价')
    hold_days = float(config.get('strategy', {}).get('hold_days', 7))

    rows = scrape_exchange(config)
    by_key = {key_for(x): x for x in rows}
    qualified = [x for x in rows if qualify(x, filters)]
    qualified.sort(key=lambda x: (x['ratio'], -(x['volume'] or 0)))

    changed = False

    for item in qualified:
        key = key_for(item)
        if key not in candidates:
            unlock = now_utc() + timedelta(days=hold_days)
            candidates[key] = {
                'name': item['name'],
                'platform': item['platform'],
                'steam_url': item['steam_url'],
                'first_seen': now_utc().isoformat(),
                'unlock_at': unlock.isoformat(),
                'discovery_ratio': item['ratio'],
                'discovery_change_7d': item['change_7d'],
                'status': 'waiting',
            }
            telegram_send(token, chat_id, discovery_message(item, unlock, sale_method))
            changed = True

    # Maturity checks: send once for candidates that reached their planned sell date.
    for key, record in list(candidates.items()):
        if record.get('status') != 'waiting':
            continue
        try:
            unlock_at = datetime.fromisoformat(record['unlock_at'])
        except Exception:
            continue
        if now_utc() < unlock_at:
            continue
        item = by_key.get(key)
        if item is None:
            item = dict(record)
            item.update({'ratio': None, 'change_7d': None, 'platform_price': None, 'steam_price': None, 'steam_balance': None, 'volume': None})
        telegram_send(token, chat_id, mature_message(item, record, sale_method))
        record['status'] = 'matured'
        record['matured_at'] = now_utc().isoformat()
        changed = True

    # Keep state bounded.
    max_records = int(config.get('state', {}).get('max_records', 3000))
    if len(candidates) > max_records:
        matured = [(k, v) for k, v in candidates.items() if v.get('status') == 'matured']
        matured.sort(key=lambda kv: kv[1].get('matured_at', ''))
        for k, _ in matured[: max(0, len(candidates) - max_records)]:
            candidates.pop(k, None)
            changed = True

    if changed:
        save_json(DATA_FILE, state)

    print(json.dumps({
        'scraped': len(rows),
        'qualified': len(qualified),
        'new_candidates': sum(1 for v in candidates.values() if v.get('status') == 'waiting' and v.get('first_seen', '').startswith(now_utc().date().isoformat())),
    }, ensure_ascii=False))


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(1)
