# -*- coding: utf-8 -*-
"""部署目标常量断言（防回归）：deploy_pages 的 API / BRANCH。

历史 bug：``API`` 被误指回原仓库 daily_stock_analysis，导致本仓库的
gh-pages 部署请求打到了上游仓库的内容接口，污染其 gh-pages 并令本仓库
页面 404。此测试常驻守护，防止该常量再次被改错（P0 阻断）。

运行（项目根目录）：
    python3 -m pytest tests/test_deploy_target.py -q
"""

from __future__ import annotations

import os
import sys

# 与 tests/test_xss_escape.py 保持一致：把 scripts/ 加入 sys.path 再 import。
# deploy_pages 已用 __name__ == "__main__" 守卫 token 校验，离线 import 不会失败。
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))

from deploy_pages import API, BRANCH  # noqa: E402


def test_deploy_api_points_to_dsa_test_repo() -> None:
    """部署目标必须指向本仓库 daily_stock_analysis_test，而非原仓库。"""
    assert API.endswith('/daily_stock_analysis_test/contents'), API


def test_deploy_branch_is_gh_pages() -> None:
    """部署分支必须为 gh-pages，才能正确生成 GitHub Pages。"""
    assert BRANCH == 'gh-pages', BRANCH
