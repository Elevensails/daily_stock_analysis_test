# -*- coding: utf-8 -*-
"""验证 U2 方案C 的 dashboard 404 修复 —— 内容排版分离后盯盘页迁至 Vite 构建产物。

断言（全部离线、无需 npm 构建）：
  * 仓库根 dashboard.html 已删除（旧死链消除）
  * web/dashboard.html 入口存在于 Vite MPA 的 rollup input（→ 构建产出 dist/dashboard.html）
  * web/src/pages/dashboard.ts 存在（盯盘渲染逻辑）
  * 若 web/dist 已构建，则 dist/dashboard.html 存在（链接可被解析、404 消失）

依据 U2-architecture.md §4：根 dashboard.html 从不随旧 deploy 推送 → 404；
方案 C 下它成为构建产物，链接自然有效。
"""

from __future__ import annotations

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
_WEB = os.path.join(_ROOT, "web")
_VITE = os.path.join(_WEB, "vite.config.ts")


def test_repo_root_dashboard_html_deleted():
    """仓库根 dashboard.html 必须已删除（修复死链的根因）。"""
    assert not os.path.exists(os.path.join(_ROOT, "dashboard.html")), \
        "仓库根 dashboard.html 应已删除，否则会重新产生 404 死链"


def test_dashboard_is_vite_mpa_entry():
    """web/dashboard.html 必须登记为 Vite rollup input，才会被构建成 dist/dashboard.html。"""
    assert os.path.isfile(os.path.join(_WEB, "dashboard.html")), "web/dashboard.html 入口缺失"
    text = open(_VITE, "r", encoding="utf-8").read()
    assert re.search(
        r"dashboard\s*:\s*resolve\(\s*webDir,\s*['\"]dashboard\.html['\"]\s*\)",
        text,
    ), "vite.config.ts 的 rollupOptions.input 未包含 dashboard 入口"


def test_dashboard_page_renderer_exists():
    """盯盘渲染逻辑 web/src/pages/dashboard.ts 必须存在。"""
    assert os.path.isfile(os.path.join(_WEB, "src", "pages", "dashboard.ts")), \
        "web/src/pages/dashboard.ts 缺失"


def test_dist_dashboard_html_built_when_dist_present():
    """若 web/dist 已构建，则 dist/dashboard.html 必须存在（链接可解析、404 消失）。"""
    dist_dashboard = os.path.join(_WEB, "dist", "dashboard.html")
    if os.path.isdir(os.path.join(_WEB, "dist")):
        assert os.path.isfile(dist_dashboard), \
            "web/dist 已构建但缺少 dashboard.html，盯盘链接仍会 404"
    else:
        # 未构建时跳过：CI 会先 `npm run build`，此处仅做结构性校验（上面两个用例已覆盖）。
        import pytest
        pytest.skip("web/dist 尚未构建，跳过产物检查（结构性校验已由前序用例覆盖）")
