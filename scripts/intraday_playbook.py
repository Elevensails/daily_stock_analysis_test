#!/usr/bin/env python3
"""
盘中 5 时段买卖剧本生成器
在 GitHub Actions 中运行，用 akshare 拉实时行情，生成条件式买卖剧本 HTML，推送到 gh-pages。

用法:
  python scripts/intraday_playbook.py --time 09:45

环境变量:
  STOCK_LIST: 逗号分隔的股票代码（如 600036,159915,603823,512400）
  GITHUB_TOKEN: GitHub PAT（用于推送到 gh-pages 分支）
"""
import os, sys, json, argparse, urllib.request, urllib.parse, urllib.error, base64
from datetime import datetime, timezone, timedelta

# U16：允许直接以脚本方式运行（scripts/ 下）时也能 import src 配置层。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import get_config

# ── 配置 ──────────────────────────────────────────────
BEIJING_TZ = timezone(timedelta(hours=8))
OWNER = 'Elevensails'
REPO = 'daily_stock_analysis'
API_BASE = 'https://api.github.com/repos/{}/{}'.format(OWNER, REPO)

# 5 时段定义
SEGMENTS = {
    "09:45": {"label": "开盘定势段", "role": "定今日基调与关键价位",
              "intro": "开盘定势：根据早盘强度与板块确认，制定今日主策略与关键价位，作为全天做T的基准。"},
    "10:30": {"label": "早盘博弈段", "role": "第一做T窗口（高抛/低吸）",
              "intro": "早盘博弈：第一做T窗口，观察早盘冲高/回踩，给出高抛低吸的具体触发条件。"},
    "13:30": {"label": "午后开局段", "role": "午后方向确认与二次做T",
              "intro": "午后开局：午后方向选择，确认是否延续早盘策略或切换，捕捉二次做T机会。"},
    "14:30": {"label": "尾盘定段",   "role": "去留决策（是否回补过夜）",
              "intro": "尾盘定段：尾盘去留决策，判断是否回补做T仓位过夜，控制隔夜风险。"},
    "14:55": {"label": "收盘前段",   "role": "隔夜仓位管理与止损复核",
              "intro": "收盘前：隔夜仓位管理与止损复核，锁定当日操作结论并预告明日关注位。"},
}

# 持仓信息映射（代码 → 名称）
STOCK_NAMES = {
    '600036': '招商银行', '159915': '创业板ETF易方达',
    '603823': '百合花', '512400': '有色金属ETF南方',
}

# ── 数据获取 ──────────────────────────────────────────
def fetch_quotes(stock_codes):
    """用东方财富 push2 API 拉实时行情（无需 akshare 依赖，直接 HTTP）"""
    secids = []
    for code in stock_codes:
        prefix = '1' if code.startswith('6') else '0'
        secids.append('{}.{}'.format(prefix, code))
    url = 'https://push2.eastmoney.com/api/qt/ulist.np/get?fields=f2,f3,f4,f5,f6,f12,f14,f15,f16,f17&secids=' + ','.join(secids)
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=8) as resp:
            d = json.loads(resp.read())
        result = {}
        for it in (d.get('data', {}) or {}).get('diff', []):
            code = it.get('f12', '')
            result[code] = {
                'name': it.get('f14', ''),
                'price': it.get('f2', 0) / 100,        # 放大100倍还原
                'chg_pct': it.get('f3', 0) / 100,       # 放大100倍还原
                'chg_amt': it.get('f4', 0) / 100,
                'volume': it.get('f5', 0),
                'amount': it.get('f6', 0),
                'high': it.get('f15', 0) / 100,
                'low': it.get('f16', 0) / 100,
                'open': it.get('f17', 0) / 100,
            }
        return result
    except Exception as e:
        print('fetch_quotes error: {}'.format(e))
        return {}

def fetch_indices():
    """拉三大指数"""
    url = 'https://push2.eastmoney.com/api/qt/ulist.np/get?fields=f2,f3,f4,f12,f14&secids=1.000001,0.399001,0.399006'
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=8) as resp:
            d = json.loads(resp.read())
        result = {}
        for it in (d.get('data', {}) or {}).get('diff', []):
            result[it.get('f14', '')] = {
                'price': it.get('f2', 0) / 100,
                'chg_pct': it.get('f3', 0) / 100,
            }
        return result
    except Exception as e:
        print('fetch_indices error: {}'.format(e))
        return {}

def fetch_sector_top(n=8):
    """拉板块涨幅 Top"""
    url = 'https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz={}&po=1&np=1&fields=f2,f3,f12,f14&fid=f3&fs=m:90+t:2'.format(n)
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=8) as resp:
            d = json.loads(resp.read())
        return [{'name': it.get('f14', ''), 'chg_pct': it.get('f3', 0) / 100} for it in (d.get('data', {}) or {}).get('diff', [])]
    except Exception as e:
        print('fetch_sector_top error: {}'.format(e))
        return []

# ── 买卖剧本生成 ──────────────────────────────────────
def build_playbook(seg, stock_data):
    """按时段生成条件式买卖剧本"""
    price = stock_data.get('price')
    chg = stock_data.get('chg_pct', 0)
    high = stock_data.get('high', price)
    low = stock_data.get('low', price)
    open_p = stock_data.get('open', price)

    # 简化版关键价位计算（无成本价时用日内高低点推算）
    if price and high and low:
        pressure = high  # 日内高点作为压力
        support = low    # 日内低点作为支撑
        break_line = low * 0.98  # 破位线 = 低点 -2%
    else:
        pressure = support = break_line = None

    def fmt(v):
        return '{:.2f}'.format(v) if v else '--'

    steps = []
    if seg == '09:45':
        if chg > 2:
            steps.append('今日主策略：高抛低吸——开盘强势冲高，冲高至 {} 附近减仓 1/3 做T'.format(fmt(pressure)))
        elif chg > 0:
            steps.append('今日主策略：持有做T——小幅高开，震荡区间内高抛低吸')
        elif chg > -2:
            steps.append('今日主策略：观望偏守——低开后观察是否企稳 {} 支撑'.format(fmt(support)))
        else:
            steps.append('今日主策略：防守减仓——大幅低开，反弹即减，不追高')
        steps.append('关键价位：压力 ≈ {} · 支撑 ≈ {} · 破位线 ≈ {}'.format(fmt(pressure), fmt(support), fmt(break_line)))
        steps.append('买入：回踩 {} 不破、量能配合 → 分批低吸'.format(fmt(support)))
        steps.append('卖出：冲高至 {} 附近、强度未放大 → 减仓 1/3'.format(fmt(pressure)))
        steps.append('风控：跌破 {} 减仓止损'.format(fmt(break_line)))
    elif seg == '10:30':
        if price and pressure and price >= pressure * 0.99:
            steps.append('当前已至压力区 {} 附近：强度未放大 → 减仓 1/3 做 T（高抛），回落至 {} 买回'.format(fmt(pressure), fmt(support)))
        elif price and support and price <= support * 1.01:
            steps.append('当前回踩支撑区 {} 附近：量能配合 → 分批买回（低吸），跌破 {} 放弃'.format(fmt(support), fmt(break_line)))
        else:
            steps.append('当前震荡区：上冲压力 {} 减、下探支撑 {} 买，等待方向明朗'.format(fmt(pressure), fmt(support)))
        steps.append('风控：跌破 {} 减仓止损'.format(fmt(break_line)))
    elif seg == '13:30':
        steps.append('午后若放量突破 {} → 持有/轻加，不追高'.format(fmt(pressure)))
        steps.append('若走弱跌破日内均线、跌幅扩大 → 减仓')
        steps.append('回踩 {} 企稳 → 二次买回做 T'.format(fmt(support)))
        steps.append('风控：跌破 {} 减仓止损'.format(fmt(break_line)))
    elif seg == '14:30':
        steps.append('日内若已高抛获利 → 尾盘不急回补，留现金防隔夜风险')
        steps.append('若强势横盘且板块配合 → 可回补至原仓位过夜')
        steps.append('破 {} → 必须减，不过夜风险仓'.format(fmt(break_line)))
        steps.append('风控：跌破 {} 减仓止损'.format(fmt(break_line)))
    elif seg == '14:55':
        steps.append('隔夜仓位：强势可留；弱势减至半仓以下')
        steps.append('止损复核：持仓若破 {} 必须减'.format(fmt(break_line)))
        steps.append('明日计划：关注 {} 突破 / {} 支撑'.format(fmt(pressure), fmt(support)))
        steps.append('风控：跌破 {} 减仓止损'.format(fmt(break_line)))
    return steps

# ── HTML 生成 ─────────────────────────────────────────
def generate_html(seg_time, stock_codes, quotes, indices, sectors):
    now = datetime.now(BEIJING_TZ)
    ts = now.strftime('%Y-%m-%d %H:%M:%S')
    seg = SEGMENTS.get(seg_time, {"label": "盘中", "role": "综合评估", "intro": ""})

    # 指数 HTML
    idx_html = ''
    for name, data in indices.items():
        chg = data['chg_pct']
        cls = 'up' if chg >= 0 else 'dn'
        sign = '+' if chg >= 0 else ''
        idx_html += '<div class="idx-card"><div class="n">{}</div><div class="p {}">{:.2f}</div><div class="c {}">{}{:.2f}%</div></div>'.format(
            name, cls, data['price'], cls, sign, chg)

    # 持仓剧本 HTML
    hold_html = ''
    for code in stock_codes:
        data = quotes.get(code, {})
        name = STOCK_NAMES.get(code, data.get('name', code))
        price = data.get('price', 0)
        chg = data.get('chg_pct', 0)
        cls = 'up' if chg >= 0 else 'dn'
        sign = '+' if chg >= 0 else ''
        steps = build_playbook(seg_time, data)
        steps_html = ''.join('<div class="step">{}</div>'.format(s) for s in steps)
        hold_html += '''
        <div class="hold-card">
          <div class="hn">{} ({})</div>
          <div class="hp {}">{:.2f} <span class="chg">{}{:.2f}%</span></div>
          <div class="steps">{}</div>
        </div>'''.format(name, code, cls, price, sign, chg, steps_html)

    # 板块 HTML
    sec_html = ''
    max_chg = max([abs(s['chg_pct']) for s in sectors] + [1])
    for s in sectors:
        chg = s['chg_pct']
        w = abs(chg) / max_chg * 100
        cls = 'up' if chg >= 0 else 'dn'
        bg = '#dc2626' if chg >= 0 else '#16a34a'
        sign = '+' if chg >= 0 else ''
        sec_html += '<div class="bar"><span class="bn">{}</span><span class="bbar"><span class="bt" style="width:{:.0f}%;background:{}"></span></span><span class="bv {}">{}{:.2f}%</span></div>'.format(
            s['name'], w, bg, cls, sign, chg)

    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>盘中追踪 {} | A股智能分析</title>
<style>
:root{{--accent:#1e40af;--bg:#f5f7fa;--card:#fff;--mut:#64748b;--red:#dc2626;--green:#16a34a;--line:#e5e7eb}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;font-size:15px;line-height:1.7;color:#1f2937}}
.wrap{{max-width:1080px;margin:0 auto;padding:0 20px 60px}}
.back{{position:sticky;top:0;z-index:100;background:rgba(255,255,255,.95);backdrop-filter:blur(8px);padding:10px 20px;border-bottom:1px solid var(--line);margin:0 -20px 16px}}
.back a{{display:inline-flex;align-items:center;gap:6px;background:linear-gradient(135deg,#1e3a5f,#1e40af);color:#fff;text-decoration:none;border-radius:8px;padding:8px 16px;font-size:13px;font-weight:600}}
header{{background:linear-gradient(135deg,#1e3a5f,#1e40af);color:#fff;padding:20px 0 16px}}
header h1{{margin:0 0 4px;font-size:22px}}
header .sub{{font-size:13px;opacity:.9}}
.module{{background:var(--card);border-radius:14px;padding:16px 18px;margin:12px 0;box-shadow:0 2px 10px rgba(30,64,175,.06)}}
.module h2{{font-size:16px;margin:0 0 10px;color:var(--accent);border-left:4px solid var(--accent);padding-left:8px}}
.idx-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
.idx-card{{background:#f8fafc;border:1px solid var(--line);border-radius:9px;padding:10px 12px}}
.idx-card .n{{font-size:12px;color:var(--mut)}}
.idx-card .p{{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums}}
.idx-card .c{{font-size:13px;font-weight:600}}
.hold-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}}
.hold-card{{background:#f8fafc;border:1px solid var(--line);border-radius:9px;padding:14px 16px}}
.hold-card .hn{{font-weight:700;font-size:14px;color:var(--accent)}}
.hold-card .hp{{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;margin:4px 0 8px}}
.hold-card .hp .chg{{font-size:14px;margin-left:8px}}
.hold-card .steps{{margin-top:8px}}
.hold-card .step{{font-size:13px;color:#374151;padding:3px 0;border-bottom:1px dashed #f1f5f9}}
.hold-card .step:last-child{{border-bottom:none}}
.bar{{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:13px}}
.bn{{width:90px;flex:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.bbar{{flex:1 1 auto;height:14px;background:#f1f5f9;border-radius:7px;overflow:hidden;min-width:0}}
.bt{{display:block;height:100%;border-radius:7px}}
.bv{{width:75px;flex:none;text-align:right;font-variant-numeric:tabular-nums}}
.up{{color:var(--red)}}.dn{{color:var(--green)}}
footer{{margin-top:24px;color:var(--mut);font-size:12px;text-align:center;border-top:1px solid var(--line);padding-top:12px}}
</style>
</head>
<body>
<div class="back"><a href="index.html">&larr; 返回首页</a></div>
<header><div class="wrap">
  <h1>🕐 盘中追踪 · {}</h1>
  <div class="sub">{} · {} · Agent 实时研究</div>
</div></header>
<div class="wrap">
  <div class="module"><h2>📈 指数实时</h2><div class="idx-grid">{}</div></div>
  <div class="module"><h2>💼 持仓买卖剧本 · {}</h2><div class="hold-grid">{}</div></div>
  <div class="module"><h2>🔥 板块涨幅 Top</h2>{}</div>
  <footer>生成时间: {} (北京) · 数据源: 东方财富 push2 API · 以上分析基于公开数据，不构成投资建议</footer>
</div>
</body>
</html>'''.format(
        seg_time, seg['label'], ts, seg['role'], seg['intro'],
        idx_html, seg['label'], hold_html, sec_html or '<div style="color:var(--mut)">数据加载失败</div>',
        ts
    )
    return html

# ── 推送到 gh-pages ───────────────────────────────────
def gh_put(path, content_str, branch='gh-pages'):
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        print('GITHUB_TOKEN not set, skip push')
        return 0
    headers = {'Authorization': 'Bearer {}'.format(token), 'Content-Type': 'application/json'}
    api = '{}/contents/{}'.format(API_BASE, urllib.parse.quote(path))
    content_b64 = base64.b64encode(content_str.encode('utf-8')).decode('ascii')
    # 获取现有 SHA
    sha = None
    try:
        r = urllib.request.Request('{}?ref={}'.format(api, branch), headers=headers)
        with urllib.request.urlopen(r, timeout=10) as resp:
            sha = json.loads(resp.read()).get('sha')
    except:
        pass
    payload = {'message': 'sync {} @ {}'.format(path, datetime.now(BEIJING_TZ).strftime('%H:%M')), 'content': content_b64, 'branch': branch}
    if sha:
        payload['sha'] = sha
    r = urllib.request.Request(api, data=json.dumps(payload).encode('utf-8'), headers=headers, method='PUT')
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        print('gh_put error: {} {}'.format(e.code, e.read().decode()[:200]))
        return e.code

# ── 主函数 ────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--time', default='09:45', help='时段 09:45/10:30/13:30/14:30/14:55')
    args = ap.parse_args()

    stock_list = os.environ.get('STOCK_LIST') or get_config().default_stock_list
    stock_codes = [c.strip() for c in stock_list.split(',') if c.strip()]
    print('持仓: {}'.format(stock_codes))

    # 拉数据
    quotes = fetch_quotes(stock_codes)
    indices = fetch_indices()
    sectors = fetch_sector_top(8)
    print('拉到 {} 只持仓 + {} 指数 + {} 板块'.format(len(quotes), len(indices), len(sectors)))

    # 生成 HTML
    html = generate_html(args.time, stock_codes, quotes, indices, sectors)

    # 推送到 gh-pages
    now = datetime.now(BEIJING_TZ)
    filename = 'intraday_{}_{}.html'.format(args.time.replace(':', ''), now.strftime('%Y%m%d'))
    status = gh_put(filename, html)
    print('推送 {} : HTTP {}'.format(filename, status))

    # 更新 index.html 中的盘中追踪链接
    index_html = generate_index_with_intraday(args.time, filename, now)
    status2 = gh_put('index.html', index_html)
    print('更新 index.html: HTTP {}'.format(status2))

def generate_index_with_intraday(seg_time, intraday_file, now):
    """生成包含盘中追踪链接的 index.html（用字符串拼接，不用 .format 避免 CSS {} 冲突）"""
    ts = now.strftime('%Y-%m-%d %H:%M')
    parts = []
    parts.append('<!DOCTYPE html>')
    parts.append('<html lang="zh-CN"><head>')
    parts.append('<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">')
    parts.append('<title>A股智能分析 · 决策仪表盘</title>')
    parts.append('<style>')
    parts.append('body{margin:0;font-family:-apple-system,sans-serif;background:#f5f7fa;color:#1f2937}')
    parts.append('.wrap{max-width:1080px;margin:0 auto;padding:40px 20px}')
    parts.append('header{background:linear-gradient(135deg,#1e3a5f,#1e40af);color:#fff;padding:24px 0 20px;border-radius:14px;text-align:center;margin-bottom:20px}')
    parts.append('header h1{margin:0 0 6px;font-size:24px}header .sub{opacity:.92;font-size:13px}')
    parts.append('.card{background:#fff;border:1px solid #e5e7eb;border-radius:11px;padding:16px 18px;margin:12px 0;text-decoration:none;color:inherit;transition:.15s;display:block}')
    parts.append('.card:hover{transform:translateY(-2px);box-shadow:0 6px 16px rgba(0,0,0,.08)}')
    parts.append('.card .t{font-size:16px;font-weight:700;color:#1e40af}.card .d{font-size:13px;color:#64748b;margin-top:4px}')
    parts.append('.card.live{border-left:4px solid #ca8a04;background:linear-gradient(135deg,#fef9c3,#fff)}')
    parts.append('.card.intraday{border-left:4px solid #1e40af}')
    parts.append('.card.quant{border-left:4px solid #7c3aed;background:linear-gradient(135deg,#f5f3ff,#fff)}')
    parts.append('.card.report{border-left:4px solid #16a34a}')
    parts.append('footer{margin-top:30px;color:#64748b;font-size:12px;text-align:center}')
    parts.append('</style></head><body>')
    parts.append('<div class="wrap">')
    parts.append('<header><h1>&#127919; A股智能分析 · 决策仪表盘</h1>')
    parts.append('<div class="sub">' + ts + ' · DeepSeek AI · 4 只持仓 · GitHub Actions 自动生成</div></header>')
    parts.append('<a class="card live" href="dashboard.html"><div class="t">&#9889; 实时盯盘</div><div class="d">30 秒自动刷新 · 持仓 + 指数 + 板块</div></a>')
    parts.append('<a class="card intraday" href="' + intraday_file + '"><div class="t">&#128336; 盘中追踪 · ' + seg_time + '</div><div class="d">分时段买卖剧本 · 条件式信号</div></a>')
    parts.append('<a class="card report" href="report_20260723.html"><div class="t">&#128202; 每日分析报告</div><div class="d">决策仪表盘 · 舆情/业绩/技术面</div></a>')
    parts.append('<a class="card quant" href="quant.html"><div class="t">&#128200; 量化分析 · Vibe-Trading</div><div class="d">多智能体辩论 · 策略回测</div></a>')
    parts.append('<footer>Fork 自 ZhuLinsen/daily_stock_analysis (49K stars) · 月成本&asymp;0<br>以上分析基于公开数据，不构成投资建议</footer>')
    parts.append('</div></body></html>')
    return '\n'.join(parts)

if __name__ == '__main__':
    main()
