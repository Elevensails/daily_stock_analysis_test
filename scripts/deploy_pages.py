#!/usr/bin/env python3
"""Deploy the built frontend (web/dist) to GitHub Pages (U2 方案C).

This script is now a PURE DEPLOYER: it only pushes the static `web/dist/`
tree produced by the Vite build to the gh-pages branch. All page assembly and
content rendering live elsewhere (the Vite frontend + emit_frontend_artifacts.py),
so this file carries no analysis or presentation logic.

Constants `API` / `BRANCH` and the `gh_*` helpers are preserved unchanged —
`tests/test_deploy_target.py` and `tests/test_xss_escape.py` depend on them.
"""
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
HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Content-Type': 'application/json',
    # The Pages API (and several others) require this media-type header;
    # without it the API returns 403 ("Resource not accessible by integration")
    # even when the token has `pages: write`. This was the root cause of the
    # "FAILED to enable Pages: status=403" on first deploy.
    'Accept': 'application/vnd.github+json',
}

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


def ensure_gh_pages_branch() -> bool:
    """Ensure the gh-pages branch exists; create an orphan on first deploy.

    First-deploy bootstrap: the Contents API ``PUT /contents/{path}`` only
    *updates* files. When gh-pages does not yet exist, every PUT 404s, nothing
    is committed, the branch is never created, and GitHub Pages 404s forever.
    We create an orphan branch via the Git Data API *with a real ``.nojekyll``
    blob* (a non-empty tree). The previous implementation posted an empty tree
    (``{'tree': []}``) which GitHub rejects with HTTP **422**, so the branch was
    never created. A non-empty tree avoids that.

    The CI ``git`` step (route b) normally pre-creates the branch before this
    script runs, so this is a robust fallback.

    Returns ``True`` if the branch exists after this call (present or just
    created), ``False`` if creation failed.
    """
    branches_url = _api_url(f'/branches/{BRANCH}')
    status, _ = _gh_call(branches_url, 'GET')
    if status == 200:
        print('  gh-pages branch already exists')
        return True
    # 404 (or other) → create an orphan via the Git Data API.
    print(f'  gh-pages branch missing (status={status}); creating orphan via Git Data API...')
    base = _api_url('')
    # a. a .nojekyll blob (empty content) so the resulting tree is non-empty.
    blob_status, blob_body = _gh_call(
        f'{base}/git/blobs', 'POST',
        {'content': '', 'encoding': 'utf-8'},
    )
    if blob_status != 201 or not blob_body:
        print(f'  FAILED to create .nojekyll blob: status={blob_status}')
        return False
    blob_sha = blob_body.get('sha')
    # b. a tree containing that blob (non-empty → avoids the 422 on empty trees).
    tree_status, tree_body = _gh_call(
        f'{base}/git/trees', 'POST',
        {'tree': [{'path': '.nojekyll', 'mode': '100644', 'type': 'blob', 'sha': blob_sha}]},
    )
    if tree_status != 201 or not tree_body:
        print(f'  FAILED to create tree: status={tree_status}')
        return False
    tree_sha = tree_body.get('sha')
    # c. an orphan commit (no parents).
    commit_status, commit_body = _gh_call(
        f'{base}/git/commits', 'POST',
        {'message': 'init gh-pages (nojekyll)', 'tree': tree_sha, 'parents': []},
    )
    if commit_status != 201 or not commit_body:
        print(f'  FAILED to create commit: status={commit_status}')
        return False
    commit_sha = commit_body.get('sha')
    # d. create the ref.
    ref_status, _ = _gh_call(
        f'{base}/git/refs', 'POST',
        {'ref': f'refs/heads/{BRANCH}', 'sha': commit_sha},
    )
    if ref_status == 201:
        print(f'  gh-pages branch created (commit {str(commit_sha)[:8]})')
        return True
    if ref_status == 422:
        # Ref may already exist (concurrent run / race) — verify instead of fail.
        verify_status, _ = _gh_call(branches_url, 'GET')
        if verify_status == 200:
            print('  gh-pages branch created (verified after 422)')
            return True
    print(f'  FAILED to create ref: status={ref_status}')
    return False


def ensure_pages_enabled() -> bool:
    """Enable GitHub Pages (serving gh-pages) if not already enabled.

    Without this the deployed files exist on gh-pages but the site is never
    served, so the page still 404s. Idempotent: a 200 (already on), 201
    (just enabled) or 409 (concurrent enable) are all treated as success.
    The ``Accept: application/vnd.github+json`` header (set in HEADERS) is
    required — without it the Pages API returns 403 ("Resource not accessible
    by integration") even when the token has ``pages: write``.

    This uses the workflow-provided ``GITHUB_TOKEN`` (the ``TOKEN`` global,
    sourced from the environment). The workflow sets
    ``permissions: pages: write`` so that token carries the ``pages`` scope;
    this is preferred over any long-lived PAT, which typically lacks the
    ``pages`` scope and would 403.

    Returns ``True`` if Pages is enabled after this call.
    """
    pages_url = _api_url('/pages')
    status, _ = _gh_call(pages_url, 'GET')
    if status == 200:
        print('  GitHub Pages enabled')
        return True
    if status == 404:
        body = {'source': {'branch': BRANCH, 'path': '/'}, 'build_type': 'legacy'}
        p_status, _ = _gh_call(pages_url, 'POST', body)
        if p_status in (200, 201):
            print('  GitHub Pages enabled (branch=gh-pages)')
            return True
        if p_status == 409:
            print('  GitHub Pages enabled (concurrent enable)')
            return True
        if p_status == 403:
            # The token may still lack pages:write at runtime (e.g. a repo-level
            # "Workflow permissions" downgrade, or a transient race). Re-check:
            # if Pages was enabled concurrently or by an external action, treat
            # it as success rather than failing the whole deploy.
            re_status, _ = _gh_call(pages_url, 'GET')
            if re_status == 200:
                print('  GitHub Pages enabled (verified after 403)')
                return True
            print('  WARNING: could not enable Pages (status=403); '
                  'verify repo "Workflow permissions" grants pages:write')
            return False
        print(f'  FAILED to enable Pages: status={p_status}')
        return False
    print(f'  skipping Pages enable, unexpected status={status}')
    return False


def gh_put(path, content_str, sha=None):
    """Create or update a file on the gh-pages branch (first-deploy safe).

    The GitHub Contents API exposes a SINGLE verb for both create and update:
    ``PUT /repos/{owner}/{repo}/contents/{path}`` (201 created / 200 updated).
    ``POST`` to that route is NOT supported and returns 404 — which previously
    meant every first-deploy file write silently failed and the site 404'd.

    We therefore ALWAYS use PUT. We read the current sha via
    ``GET /contents/{path}?ref=gh-pages`` first; when the file already exists
    we attach its ``sha`` (update), otherwise we omit it (create). The caller
    may pass ``sha`` pre-fetched via ``gh_get_sha``; if omitted we fetch it.
    """
    b64 = base64.b64encode(content_str.encode('utf-8')).decode('ascii')
    if sha is None:
        sha = gh_get_sha(path)
    payload = {'message': f'deploy {path}', 'content': b64, 'branch': BRANCH}
    if sha:
        payload['sha'] = sha
    # PUT is the only valid verb for create AND update on the Contents API.
    req = urllib.request.Request(f'{API}/{path}', data=json.dumps(payload).encode('utf-8'), headers=HEADERS, method='PUT')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        print(f'  HTTP {e.code} for {path} (PUT)'); return e.code

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

def gh_delete(path, sha):
    """Delete a file on the gh-pages branch (used to clean stale legacy files)."""
    if not sha:
        return False
    req = urllib.request.Request(f'{API}/{path}?ref={BRANCH}', headers=HEADERS, method='DELETE')
    req.data = json.dumps({'message': f'remove {path}', 'sha': sha, 'branch': BRANCH}).encode('utf-8')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status in (200, 204)
    except urllib.error.HTTPError as e:
        print(f'  HTTP {e.code} deleting {path} (skipped)')
        return False

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

def _walk_dist(dist_dir):
    """Yield all file paths (repo-relative, '/'-separated) under dist_dir."""
    out = []
    for root, _, fnames in os.walk(dist_dir):
        for fn in fnames:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, dist_dir).replace(os.sep, '/')
            out.append(rel)
    return out

def _push_tree(dist_dir):
    """Upload the entire dist tree to gh-pages via gh_put (create/update)."""
    paths = _walk_dist(dist_dir)
    for rel in paths:
        with open(os.path.join(dist_dir, rel), 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        sha = gh_get_sha(rel)
        status = gh_put(rel, content, sha)
        print(f'  {status} {rel}')

def _build_reports_index(manifest):
    """From today's manifest build {date: {'slots':[...], 'fragments':[...]}}."""
    entries = {}
    slots = (manifest or {}).get('slots', {}) or {}
    for slot_code, slot in slots.items():
        frags = (slot or {}).get('fragments', {}) or {}
        files = [v for v in frags.values() if v]
        if not files:
            continue
        date = None
        for f in files:
            m = re.search(r'_(\d{8})\.html$', f)
            if m:
                date = m.group(1)
                break
        if not date:
            continue
        e = entries.setdefault(date, {'slots': [], 'fragments': []})
        if slot_code not in e['slots']:
            e['slots'].append(slot_code)
        for f in files:
            if f not in e['fragments']:
                e['fragments'].append(f)
    return entries

def _merge_reports_index(existing, new_entries):
    """Merge new date→entry map into an existing reports-index.json dict."""
    entries = dict((existing or {}).get('entries', {}))
    for date, e in (new_entries or {}).items():
        cur = entries.get(date, {'slots': [], 'fragments': []})
        for s in e.get('slots', []):
            if s not in cur['slots']:
                cur['slots'].append(s)
        for f in e.get('fragments', []):
            if f not in cur['fragments']:
                cur['fragments'].append(f)
        entries[date] = cur
    dates = sorted(entries.keys(), reverse=True)
    return {
        'schemaVersion': '1.0',
        'updatedAt': datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M'),
        'dates': dates,
        'entries': entries,
    }

def _cleanup_stale(dist_dir):
    """Delete legacy gh-pages files superseded by the new SPA build.

    Only runs when the new SPA entry points are all present (dist is complete),
    so a partial/failed build can never wipe the live site. Always preserves
    ``.nojekyll`` and ``reports-index.json``; skips directories.
    """
    required = ['index.html', 'report.html', 'market_review.html', 'vibe.html', 'archive.html', 'dashboard.html']
    if not all(os.path.isfile(os.path.join(dist_dir, r)) for r in required):
        print('  skip cleanup (dist incomplete)')
        return
    dist_paths = set(_walk_dist(dist_dir))
    keep = {'.nojekyll', 'reports-index.json'}
    existing = gh_list_files()
    removed = 0
    for item in existing:
        name = item.get('name')
        if not name or item.get('type') == 'dir':
            continue
        if name in keep or name in dist_paths:
            continue
        if gh_delete(name, item.get('sha')):
            removed += 1
            print(f'  deleted stale: {name}')
    if removed:
        print(f'  cleaned {removed} stale file(s)')

# ====== MAIN (PURE DIST PUSHER) ======
def main():
    dist_dir = os.environ.get('DIST_DIR', os.path.join('web', 'dist'))
    if not os.path.isdir(dist_dir):
        print(f'FATAL: dist dir not found: {dist_dir} (run `npm run build` first)')
        raise SystemExit(1)
    if not ensure_gh_pages_branch():  # first-deploy bootstrap / fallback
        print('FATAL: could not ensure gh-pages branch exists')
        raise SystemExit(1)
    print(f'Pushing {dist_dir} → gh-pages ...')
    _push_tree(dist_dir)

    # Maintain reports-index.json (archive page fetches it at runtime).
    manifest_path = os.path.join('web', 'src', 'content', 'manifest.json')
    existing_idx = {}
    existing_raw = gh_get_content('reports-index.json')
    if existing_raw:
        try:
            existing_idx = json.loads(existing_raw)
        except Exception:
            existing_idx = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            new_entries = _build_reports_index(manifest)
            if new_entries:
                idx = _merge_reports_index(existing_idx, new_entries)
                gh_put('reports-index.json', json.dumps(idx, ensure_ascii=False, indent=2))
                print('  updated reports-index.json')
        except Exception as exc:
            print(f'  WARN: could not build reports-index.json: {exc}')
    elif not existing_raw:
        # Self-heal: create an empty index so the archive page never 404s.
        idx = {'schemaVersion': '1.0', 'updatedAt': '', 'dates': [], 'entries': {}}
        gh_put('reports-index.json', json.dumps(idx, ensure_ascii=False, indent=2))
        print('  created empty reports-index.json')

    # Clean stale legacy files (slot_*.html / old report_*.html / debug.json ...).
    _cleanup_stale(dist_dir)

    if not ensure_pages_enabled():  # first-deploy bootstrap: turn on GitHub Pages
        print('FATAL: could not enable GitHub Pages')
        raise SystemExit(1)
    print('deploy done')

if __name__ == '__main__': main()
