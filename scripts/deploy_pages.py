#!/usr/bin/env python3
"""Deploy reports to GitHub Pages. MD->HTML, slot pages, index, archive."""
import os, re, base64, json, html, urllib.request, urllib.error, glob
from datetime import datetime, timezone, timedelta
from collections import defaultdict

TOKEN = os.environ.get('GITHUB_TOKEN', '')
# import 守卫：仅作为脚本直接运行时才校验 token，避免 offline 单元测试在
# import deploy_pages 时因缺少 GITHUB_TOKEN 直接 SystemExit 而无法加载。
if __name__ == '__main__':
    if not TOKEN:
        print('FATAL: GITHUB_TOKEN is empty!')
        raise SystemExit(1)
    print(f'GITHUB_TOKEN: {len(TOKEN)} chars (prefix: {TOKEN[:4]}...)')
API = 'https://api.github.com/repos/Elevensails/daily_stock_analysis_test/contents'
BRANCH = 'gh-pages'
# Repo-root API base: strip the trailing '/contents' so we can reach branch/commit/Pages
# endpoints (which live outside the Contents API). Derived from API to keep a single
# source of truth — API/BRANCH themselves stay unchanged (tests assert on them).
REPO_API = API.rsplit('/contents', 1)[0]
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}

# ====== PREMIUM CSS DESIGN SYSTEM ======
BASE_CSS = '''<style>
:root {
  --pri: #1e3a5f; --pri-lt: #1e40af; --accent: #2563eb;
  --bg: #f0f4f8; --card: #fff; --card-hover: #f8fafc;
  --text: #1e293b; --text-mut: #64748b; --text-dim: #94a3b8;
  --border: #e2e8f0; --border-focus: #93c5fd;
  --green: #16a34a; --green-bg: #dcfce7;
  --red: #dc2626; --red-bg: #fee2e2;
  --amber: #d97706; --amber-bg: #fef3c7;
  --purple: #7c3aed; --purple-bg: #f3e8ff;
  --radius: 12px; --radius-sm: 8px;
  --shadow: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
  --shadow-lg: 0 4px 16px rgba(0,0,0,.08);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);font-family:-apple-system,"PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif;font-size:15px;line-height:1.75;color:var(--text);-webkit-font-smoothing:antialiased}
.wrap{max-width:960px;margin:0 auto;padding:0 20px 60px}

/* Navigation */
.nav{position:sticky;top:0;z-index:100;background:rgba(255,255,255,.92);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;gap:8px;align-items:center;margin:0 -20px 24px}
.nav a,.nav span{display:inline-flex;align-items:center;gap:5px;padding:6px 14px;border-radius:var(--radius-sm);font-size:13px;font-weight:500;text-decoration:none;transition:all .15s}
.nav a{background:var(--pri);color:#fff}
.nav a:hover{background:var(--pri-lt);transform:translateY(-1px)}
.nav a.ghost{background:transparent;color:var(--pri);border:1px solid var(--border)}
.nav a.ghost:hover{background:var(--card-hover)}
.nav .sep{color:var(--text-dim);font-size:14px;padding:0 2px}

/* Cards */
.card{display:block;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px 24px;margin:14px 0;text-decoration:none;color:inherit;transition:all .2s;box-shadow:var(--shadow)}
.card:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg);border-color:var(--border-focus)}
.card .card-title{font-size:17px;font-weight:700;color:var(--pri);margin-bottom:4px}
.card .card-desc{font-size:13px;color:var(--text-mut);line-height:1.6}
.card.pending{opacity:.45;pointer-events:none}
.card.stock{border-left:4px solid var(--accent)}
.card.market{border-left:4px solid #0d9488}
.card.quant{border-left:4px solid var(--purple);background:linear-gradient(135deg,var(--purple-bg),#fff)}
.card.live{border-left:4px solid var(--amber);background:linear-gradient(135deg,var(--amber-bg),#fff)}
.card.archive{border-left:4px solid var(--text-dim)}

.badge{display:inline-block;color:#fff;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:600;margin-left:8px;vertical-align:middle}
.badge-ok{background:var(--green)}
.badge-pending{background:var(--text-dim)}

/* Header */
.hero{background:linear-gradient(135deg,var(--pri),var(--pri-lt));color:#fff;padding:28px 24px;border-radius:var(--radius);margin-bottom:24px;text-align:center;box-shadow:var(--shadow-lg)}
.hero h1{font-size:24px;margin-bottom:6px;letter-spacing:-.3px}
.hero .meta{opacity:.88;font-size:13px;margin-top:8px}
.stats{display:flex;gap:10px;justify-content:center;margin-top:12px;flex-wrap:wrap}
.stats span{background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.25);border-radius:20px;padding:3px 14px;font-size:12px;white-space:nowrap}

/* Section */
.section-title{font-size:14px;font-weight:700;color:var(--text-mut);margin:28px 0 12px;padding-bottom:8px;border-bottom:2px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.section-title .count{font-size:12px;font-weight:500;color:var(--text-dim)}

/* Report Content */
.module{background:var(--card);border-radius:var(--radius);padding:24px 28px;margin:16px 0;box-shadow:var(--shadow)}
.module h1{font-size:22px;margin:0 0 20px;color:var(--pri);padding-bottom:12px;border-bottom:2px solid var(--border)}
.module h2{font-size:18px;margin:28px 0 14px;padding:10px 0 10px 14px;border-left:4px solid var(--accent);background:linear-gradient(90deg,#eff6ff,transparent);border-radius:0 var(--radius-sm) var(--radius-sm) 0;color:var(--pri)}
.module h3{font-size:15px;margin:20px 0 10px;color:var(--text);font-weight:700}
.module p{font-size:14px;line-height:1.8;color:var(--text);margin:0 0 10px}
.module blockquote{border-left:4px solid var(--accent);background:#eff6ff;padding:12px 16px;margin:12px 0;border-radius:0 var(--radius-sm) var(--radius-sm) 0;font-size:14px;color:var(--pri)}
.module li{font-size:14px;line-height:1.8;color:var(--text);margin:3px 0;padding-left:4px}
.module li::marker{color:var(--accent)}
.module hr{border:none;border-top:1px solid var(--border);margin:20px 0}
.module strong{color:var(--pri);font-weight:700}
.module code{background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:13px;color:var(--red)}

/* Stock section cards inside reports */
.stock-section{border:1px solid var(--border);border-radius:var(--radius);margin:20px 0;overflow:hidden}
.stock-section h2{margin:0;padding:14px 18px;background:linear-gradient(90deg,#eff6ff,#f8fafc);border-left:4px solid var(--accent);border-radius:0;font-size:16px}
.stock-section .stock-body{padding:0 18px 14px}

/* Tables */
table.tbl{width:100%;border-collapse:collapse;font-size:13px;margin:12px 0;border-radius:var(--radius-sm);overflow:hidden;box-shadow:var(--shadow)}
table.tbl th{background:var(--pri);color:#fff;padding:10px 12px;text-align:left;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.3px}
table.tbl td{padding:8px 12px;border-bottom:1px solid var(--border);background:var(--card)}
table.tbl tr:last-child td{border-bottom:none}
table.tbl tr:hover td{background:var(--card-hover)}

/* Footer */
footer{margin-top:40px;color:var(--text-dim);font-size:12px;text-align:center;border-top:1px solid var(--border);padding-top:16px}
footer a{color:var(--accent);text-decoration:none}

/* Archive page */
.archive-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.archive-item{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);padding:14px 16px;text-decoration:none;color:var(--text);transition:all .15s;box-shadow:var(--shadow)}
.archive-item:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg);border-color:var(--accent)}
.archive-item .date{font-weight:700;color:var(--pri);font-size:15px}
.archive-item .count{font-size:12px;color:var(--text-mut);margin-top:4px}
.archive-item .preview{font-size:12px;color:var(--text-mut);margin-top:6px;line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}

/* Responsive */
@media(max-width:640px){
  .wrap{padding:0 12px 40px}
  .nav{margin:0 -12px 16px;padding:10px 12px}
  .hero{padding:20px 16px;border-radius:var(--radius-sm)}
  .module{padding:16px 18px}
  .card{padding:14px 16px}
  .archive-list{grid-template-columns:1fr}
}
</style>'''

# ====== MD TO HTML ======
def md2html(md):
    """将模型生成的 Markdown 报告转为 HTML 片段。

    XSS 安全：所有来自模型/外部的文本节点（表格单元格、标题、引用、列表、段落）
    均先经 ``html.escape`` 再 f-string 注入；仅我方代码生成的标签（<strong>/<td>/
    <th>/<table> 等）不 escape。``html.escape`` 仅转义 ``< > & " '``，不影响 markdown
    强调（``**bold**`` 的 ``*`` 不被转义）。段落行必须先 escape 原始文本、再做
    ``** -> <strong>`` 替换，否则我方生成的 ``<strong>`` 会被反向破坏。
    """
    lines = md.split('\n')
    out = []; in_table = False; in_stock = False
    for line in lines:
        if line.startswith('|'):
            cells = [c.strip() for c in line.split('|')[1:-1]]
            if not in_table:
                out.append('<div style="overflow-x:auto"><table class="tbl"><thead><tr>'+''.join(f'<th>{html.escape(c)}</th>' for c in cells)+'</tr></thead><tbody>')
                in_table = True; continue
            if all(c.replace('-','').replace(':','')=='' for c in cells): continue
            out.append('<tr>'+''.join(f'<td>{html.escape(c)}</td>' for c in cells)+'</tr>')
        else:
            if in_table: out.append('</tbody></table></div>'); in_table = False
            if line.startswith('## ') and not line.startswith('### '):
                # Stock section: wrap in styled card
                if in_stock: out.append('</div></div>')
                stock_title = line[3:].strip()
                out.append(f'<div class="stock-section"><h2>{html.escape(stock_title)}</h2><div class="stock-body">')
                in_stock = True
            elif line.startswith('# '): out.append(f'<h1>{html.escape(line[2:])}</h1>')
            elif line.startswith('### '): out.append(f'<h3>{html.escape(line[4:])}</h3>')
            elif line.startswith('> '): out.append(f'<blockquote>{html.escape(line[2:])}</blockquote>')
            elif line.startswith('- ') or line.startswith('* '): out.append(f'<li>{html.escape(line[2:])}</li>')
            elif line.strip() == '---': out.append('<hr>')
            elif line.strip():
                # 先转义原始模型文本，再对转义结果做 ** -> <strong> 替换。
                safe_line = html.escape(line)
                line2 = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', safe_line)
                out.append(f'<p>{line2}</p>')
    if in_table: out.append('</tbody></table></div>')
    if in_stock: out.append('</div></div>')
    return '\n'.join(out)

# ====== GITHUB API HELPERS ======
def _gh_call(url, method, payload=None):
    """Send an authenticated GitHub API call. Returns (status, json_body).

    json_body is None when there is no decodable JSON. HTTP errors surface as
    their status code (e.g. 404/409) so callers can branch on them; transport
    errors (no network) return (0, None) so the deploy degrades gracefully.
    """
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read() if hasattr(e, 'read') else b''
    except urllib.error.URLError as e:
        print(f'  network error calling {url}: {e}')
        return (0, None)
    try:
        body = json.loads(raw) if raw else None
    except (ValueError, json.JSONDecodeError):
        body = None
    return (status, body)


def _api_url(suffix: str) -> str:
    """Build a repo-scoped GitHub API URL from the contents API base.

    API ends with '/contents'; strip it to reach the repo root, then append the
    repo-scoped ``suffix`` (e.g. '/branches/gh-pages').
    """
    return REPO_API + suffix


def ensure_gh_pages_branch() -> None:
    """Ensure the gh-pages branch exists; create an empty orphan on first deploy.

    The Contents API ``PUT /contents/{path}`` only *updates* files. On the very
    first run the gh-pages branch does not exist and no report files exist, so
    every PUT returns HTTP 404, nothing is committed, the branch is never
    created, and GitHub Pages 404s forever. We create an empty orphan branch via
    the Git Data API before uploading any files.
    """
    branches_url = _api_url(f'/branches/{BRANCH}')
    status, _ = _gh_call(branches_url, 'GET')
    if status == 200:
        print('  gh-pages branch already exists')
        return
    # 404 (or other) → create empty orphan via Git Data API.
    print(f'  gh-pages branch missing (status={status}); creating empty orphan...')
    base = _api_url('')
    # a. empty tree
    t_status, tree_body = _gh_call(f'{base}/git/trees', 'POST', {'tree': []})
    if t_status != 201 or not tree_body:
        print(f'  FAILED to create tree: status={t_status}')
        return
    tree_sha = tree_body.get('sha')
    # b. empty commit (no parents → orphan)
    c_status, commit_body = _gh_call(
        f'{base}/git/commits', 'POST',
        {'message': 'init gh-pages', 'tree': tree_sha, 'parents': []},
    )
    if c_status != 201 or not commit_body:
        print(f'  FAILED to create commit: status={c_status}')
        return
    commit_sha = commit_body.get('sha')
    # c. create the ref
    r_status, _ = _gh_call(
        f'{base}/git/refs', 'POST',
        {'ref': f'refs/heads/{BRANCH}', 'sha': commit_sha},
    )
    if r_status == 201:
        print(f'  gh-pages branch created (commit {str(commit_sha)[:8]})')
    else:
        print(f'  FAILED to create ref: status={r_status}')


def ensure_pages_enabled() -> None:
    """Enable GitHub Pages (serving gh-pages) if not already enabled.

    Without this the deployed files exist on gh-pages but the site is never
    served, so the page still 404s. Idempotent: a 200 (already on) or 409
    (concurrent enable) are both treated as success.
    """
    pages_url = _api_url('/pages')
    status, _ = _gh_call(pages_url, 'GET')
    if status == 200:
        print('  GitHub Pages already enabled')
        return
    if status == 404:
        body = {'source': {'branch': BRANCH, 'path': '/'}, 'build_type': 'legacy'}
        p_status, _ = _gh_call(pages_url, 'POST', body)
        if p_status in (200, 201):
            print('  GitHub Pages enabled (branch=gh-pages)')
        elif p_status == 409:
            print('  GitHub Pages already enabled (409)')
        else:
            print(f'  FAILED to enable Pages: status={p_status}')
    else:
        print(f'  skipping Pages enable, unexpected status={status}')


def gh_put(path, content_str, sha=None):
    """Create or update a file on the gh-pages branch (first-deploy safe).

    Before writing we read the current sha via ``GET /contents/{path}?ref=gh-pages``:
      - 200 (file exists) → ``PUT`` with sha (update, exactly as before)
      - 404 (file missing) → ``POST`` to create the file (first deploy / new page)
    The caller may pass ``sha`` pre-fetched via ``gh_get_sha``; if omitted we
    fetch it ourselves so the create-vs-update decision is always correct.
    """
    b64 = base64.b64encode(content_str.encode('utf-8')).decode('ascii')
    if sha is None:
        sha = gh_get_sha(path)
    if sha:
        payload = {'message': f'deploy {path}', 'content': b64, 'branch': BRANCH, 'sha': sha}
        method = 'PUT'
    else:
        # Create: POST (branch already exists after ensure_gh_pages_branch).
        payload = {'message': f'deploy {path}', 'content': b64, 'branch': BRANCH}
        method = 'POST'
    req = urllib.request.Request(f'{API}/{path}', data=json.dumps(payload).encode('utf-8'), headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        print(f'  HTTP {e.code} for {path} ({method})'); return e.code

def gh_get_sha(path):
    try:
        req = urllib.request.Request(f'{API}/{path}?ref={BRANCH}', headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get('sha')
    except: return None

def gh_get_content(path):
    """GET {path} from gh-pages and return decoded text, or None on failure."""
    try:
        req = urllib.request.Request(f'{API}/{path}?ref={BRANCH}', headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        content = data.get('content')
        if not content:
            return None
        return base64.b64decode(content).decode('utf-8', errors='replace')
    except: return None

def nearest_slot(tslot):
    slots = ['0900','0930','1200','1430','1800']
    t = int(tslot[:2])*60+int(tslot[2:])
    return min(slots, key=lambda s: abs((int(s[:2])*60+int(s[2:]))-t))

def gh_list_files():
    try:
        req = urllib.request.Request(f'{API}?ref={BRANCH}', headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except: return []

# ====== PURE HELPERS ======
def extract_preview(html_text, max_len=90):
    """Return a plain-text preview snippet from a report HTML string.

    Pure function: accepts already-fetched HTML text and performs no I/O.
    Strips style/script/footer/nav blocks, removes tags, collapses
    whitespace, drops navigation/footer residue, and truncates to
    ``max_len`` characters at a word boundary.
    """
    if not html_text:
        return ''
    # 1. Remove non-content block elements.
    text = re.sub(r'<style[\s\S]*?</style>', ' ', html_text, flags=re.IGNORECASE)
    text = re.sub(r'<script[\s\S]*?</script>', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'<footer[\s\S]*?</footer>', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'<nav[\s\S]*?</nav>', ' ', text, flags=re.IGNORECASE)
    # 2. Remove every remaining tag.
    text = re.sub(r'<[^>]+>', ' ', text)
    # 3. Decode entities for readability.
    text = html.unescape(text)
    # 4. Drop navigation / footer residue lines.
    skip_contains = ('不构成投资建议', '以上分析基于公开数据')
    skip_prefixes = ('首页', '历史归档', '返回', 'javascript')
    kept = []
    for ln in text.split('\n'):
        ln = ln.strip()
        if not ln:
            continue
        if any(ln.startswith(p) for p in skip_prefixes):
            continue
        if any(p in ln for p in skip_contains):
            continue
        kept.append(ln)
    text = ' '.join(kept)
    # 5. Collapse internal whitespace.
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return ''
    # 6. Truncate at a word boundary, append ellipsis if cut.
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    sp = cut.rfind(' ')
    if sp > max_len // 2:
        cut = cut[:sp]
    return cut.rstrip() + '…'

# ====== PAGE GENERATORS ======
def build_report_html(md, title, now_ts):
    """把 Markdown 报告内容拼装为完整 HTML 页面（纯函数，无 I/O）。

    仅做 HTML 拼装，不触发 ``gh_get_sha`` / ``gh_put``，便于离线测试。所有来自
    模型/外部的文本（``title``、md2html 渲染出的 body）均已先 html.escape 再注入；
    我方生成的标签（<strong>/<td> 等）与常量不 escape。
    """
    safe_title = html.escape(title)
    body = md2html(md)
    return f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>{safe_title}</title>{BASE_CSS}</head>
<body><div class="nav"><a href="index.html">&#127968; 首页</a><span class="sep">/</span><a href="archive.html">&#128451; 历史</a><span class="sep">/</span><a href="javascript:history.back()" class="ghost">&#8592; 返回</a></div>
<div class="wrap"><div class="module">{body}</div>
<footer>{now_ts} · DeepSeek AI · 以上分析基于公开数据，不构成投资建议</footer></div></body></html>'''


def make_report_page(md_file, html_name, now_ts):
    with open(md_file, 'r', encoding='utf-8') as f: md = f.read()
    title = md.split('\n')[0].replace('# ','').strip()
    html = build_report_html(md, title, now_ts)
    sha = gh_get_sha(html_name)
    return gh_put(html_name, html, sha)

def make_slot_page(tslot, time_label, slot_name, color, color_dark, today, reports_dict):
    now_ts = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    slot_data = reports_dict.get(tslot, {})
    cards = []
    items = [
        ('stock','&#128202; 个股分析','4 只持仓逐一深度分析 · 技术面/资金面/操作点位',color),
        ('market','&#127758; 大盘复盘','指数结构 · 板块主线 · 资金情绪 · 交易计划','#0d9488'),
        ('vibe','&#128200; 量化分析','Vibe-Trading 多智能体策略回测','#7c3aed'),
    ]
    for key, title, desc, border_color in items:
        if key in slot_data:
            cards.append(f'<a class="card" style="border-left:4px solid {border_color}" href="{slot_data[key]}"><div class="card-title">{title}</div><div class="card-desc">{desc}</div></a>')
        else:
            cards.append(f'<div class="card pending" style="border-left:4px solid {border_color}"><div class="card-title">{title} <span class="badge badge-pending">待生成</span></div><div class="card-desc">{desc}</div></div>')
    slot_html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>{time_label} · A股分析</title>{BASE_CSS}</head>
<body><div class="nav"><a href="index.html">&#127968; 首页</a><span class="sep">/</span><a href="archive.html">&#128451; 历史</a><span class="sep">/</span><a href="javascript:history.back()" class="ghost">&#8592; 返回</a></div>
<div class="wrap"><div class="hero" style="background:linear-gradient(135deg,{color_dark},{color})"><h1>&#128338; {time_label} · {slot_name}</h1><div class="meta">今日 · DeepSeek AI · 4 只持仓</div></div>
{chr(10).join(cards)}
<footer>以上分析基于公开数据，不构成投资建议</footer></div></body></html>'''
    sha = gh_get_sha(f'slot_{tslot}.html')
    return gh_put(f'slot_{tslot}.html', slot_html, sha)

def make_index(reports_dict, today):
    SLOTS = {
        '0900':('09:00','早盘分析','#f59e0b','#92400e'),
        '0930':('09:30','开盘追踪','#ef4444','#991b1b'),
        '1200':('12:00','午间复盘','#8b5cf6','#5b21b6'),
        '1430':('14:30','午盘追踪','#3b82f6','#1e40af'),
        '1800':('18:00','收盘复盘','#10b981','#065f46'),
    }
    # Today's slot cards
    today_cards = []
    for tslot, (label, name, color, _) in SLOTS.items():
        sd = reports_dict.get(tslot, {})
        ok = bool(sd)
        badge = '<span class="badge badge-ok">已生成</span>' if ok else '<span class="badge badge-pending">待生成</span>'
        extra = ' pending' if not ok else ''
        today_cards.append(f'<a class="card{extra}" style="border-left:4px solid {color}" href="slot_{tslot}.html"><div class="card-title">&#128338; {label} · {name} {badge}</div><div class="card-desc">个股分析 + 大盘复盘 + 量化分析</div></a>')
    generated = sum(1 for t in SLOTS if reports_dict.get(t))
    index_html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>A股智能分析 · 决策仪表盘</title>{BASE_CSS}
</head><body><div class="wrap">
<div class="hero"><h1>&#127919; A股智能分析 · 决策仪表盘</h1><div class="meta">DeepSeek AI · 4 只持仓 · 5 时段/天 · <span id="liveTime"></span></div>
<div class="stats"><span>&#9889; 5次/天</span><span>&#128187; 4 持仓</span><span>&#128196; 3 报告</span><span>&#128176; &asymp;0 月成本</span></div></div>
<div style="display:flex;gap:8px;margin-bottom:16px">
<a class="card live" href="dashboard.html" style="flex:1"><div class="card-title">&#9889; 实时盯盘</div><div class="card-desc">30s 刷新 · 4 持仓 + 指数</div></a>
<a class="card archive" href="archive.html" style="flex:1"><div class="card-title">&#128451; 历史归档</div><div class="card-desc">按日期浏览全部报告</div></a>
</div>
<div class="section-title">&#128197; 今日 5 时段分析<span class="count">{generated}/5 已生成</span></div>
{chr(10).join(today_cards)}
<footer><a href="https://github.com/Elevensails/daily_stock_analysis">Fork 自 daily_stock_analysis</a> · 09:00 早盘 / 09:30 开盘 / 12:00 午间 / 14:30 午盘 / 18:00 收盘<br>以上分析基于公开数据，不构成投资建议</footer>
<script>(function(){{var d=new Date();var bj=new Date(d.getTime()+d.getTimezoneOffset()*60000+8*3600000);var ts=bj.toISOString().replace('T',' ').slice(0,16);var el=document.getElementById('liveTime');if(el)el.textContent=ts;}})();</script>
</div></body></html>'''
    sha = gh_get_sha('index.html')
    return gh_put('index.html', index_html, sha)

def make_archive_page():
    """Generate archive.html listing all dates with reports and previews."""
    existing = gh_list_files()
    if not existing: return 404
    # Group by date: {YYYYMMDD: [report html files]}
    dates = defaultdict(list)
    for item in existing:
        name = item['name']
        m = re.match(r'(?:report|market_review|vibe)_(\d{4})_(\d{8})\.html', name)
        if m: dates[m.group(2)].append(name)
    if not dates: return 404
    sorted_dates = sorted(dates.keys(), reverse=True)
    items_html = []
    for d in sorted_dates:
        ds = f'{d[:4]}-{d[4:6]}-{d[6:]}'
        files = dates[d]
        n = len(files)
        # Choose a representative report for this date (real file on gh-pages).
        if f'market_review_1800_{d}.html' in files:
            rep = f'market_review_1800_{d}.html'
        else:
            mr = [f for f in files if 'market_review' in f]
            rep = mr[0] if mr else files[0]
        # Safe-degrade: only render preview when content is fetchable.
        preview_div = ''
        content = gh_get_content(rep)
        if content:
            preview = extract_preview(content)
            if preview:
                # Escape to avoid breaking the f-string (e.g. stray { } ).
                safe = html.escape(preview).replace('{', '&#123;').replace('}', '&#125;')
                preview_div = f'<div class="preview">{safe}</div>'
        items_html.append(f'<a class="archive-item" href="{rep}"><div class="date">&#128197; {ds}</div><div class="count">{n} 份报告</div>{preview_div}</a>')
    archive_html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>历史归档 · A股智能分析</title>{BASE_CSS}</head>
<body><div class="nav"><a href="index.html">&#127968; 首页</a><span class="sep">/</span><span>&#128451; 历史归档</span></div>
<div class="wrap"><div class="hero"><h1>&#128451; 历史归档</h1><div class="meta">共 {len(sorted_dates)} 个交易日</div></div>
<div class="archive-list">{chr(10).join(items_html)}</div>
<footer>以上分析基于公开数据，不构成投资建议</footer></div></body></html>'''
    sha = gh_get_sha('archive.html')
    return gh_put('archive.html', archive_html, sha)

# ====== MAIN ======
def main():
    reports_dir = os.environ.get('REPORTS_DIR', 'reports')
    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.strftime('%Y%m%d')
    now_ts = now.strftime('%Y-%m-%d %H:%M')
    md_files = sorted(glob.glob(os.path.join(reports_dir, '*.md')))
    print(f'Found {len(md_files)} MD files')
    ensure_gh_pages_branch()  # first-deploy bootstrap: create gh-pages if missing
    reports_dict = {}
    for f in md_files:
        bn = os.path.basename(f)
        m = re.match(r'report_(\d{4})_(\d{8})\.md', bn)
        mr = re.match(r'market_review_(\d{4})_(\d{8})\.md', bn)
        vb = re.match(r'vibe_(\d{4})_(\d{8})\.md', bn)
        if m:
            tslot = nearest_slot(m.group(1))
            hn = bn.replace('.md','.html')
            reports_dict.setdefault(tslot,{})['stock']=hn
            print(f'  {make_report_page(f,hn,now_ts)} {hn}')
        elif mr:
            tslot = nearest_slot(mr.group(1))
            hn = bn.replace('.md','.html')
            reports_dict.setdefault(tslot,{})['market']=hn
            print(f'  {make_report_page(f,hn,now_ts)} {hn}')
        elif vb:
            tslot = nearest_slot(vb.group(1))
            hn = bn.replace('.md','.html')
            reports_dict.setdefault(tslot,{})['vibe']=hn
            print(f'  {make_report_page(f,hn,now_ts)} {hn}')
        else:
            print(f'  skip: {bn}')
    # Merge existing reports from gh-pages
    existing = gh_list_files()
    if existing:
        for item in existing:
            name = item['name']
            for pat, key in [(r'report_(\d{4})_(\d{8})\.html','stock'),(r'market_review_(\d{4})_(\d{8})\.html','market'),(r'vibe_(\d{4})_(\d{8})\.html','vibe')]:
                m = re.match(pat, name)
                if m and key not in reports_dict.get(m.group(1),{}):
                    reports_dict.setdefault(m.group(1),{})[key]=name
                    print(f'  (gh-pages) {name}')
    # Generate pages
    SLOTS = {'0900':('09:00','早盘分析','#f59e0b','#92400e'),'0930':('09:30','开盘追踪','#ef4444','#991b1b'),
             '1200':('12:00','午间复盘','#8b5cf6','#5b21b6'),'1430':('14:30','午盘追踪','#3b82f6','#1e40af'),
             '1800':('18:00','收盘复盘','#10b981','#065f46')}
    for ts,(tl,sn,c,cd) in SLOTS.items():
        print(f'  {make_slot_page(ts,tl,sn,c,cd,today,reports_dict)} slot_{ts}.html')
    print(f'  {make_index(reports_dict,today)} index.html')
    print(f'  {make_archive_page()} archive.html')
    # Debug
    dbg={'md_files':len(md_files),'slots':list(reports_dict.keys())}
    gh_put('debug.json', json.dumps(dbg,indent=2))
    ensure_pages_enabled()  # first-deploy bootstrap: turn on GitHub Pages
    print('deploy done')

if __name__ == '__main__': main()
