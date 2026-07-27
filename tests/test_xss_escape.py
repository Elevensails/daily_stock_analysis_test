# -*- coding: utf-8 -*-
"""XSS 安全渲染测试（T04/U4）：deploy_pages 的 md2html / build_report_html。

验证所有模型/外部文本节点在注入 HTML 前均经过 html.escape，且合法 markdown 排版
（**bold** -> <strong>）不被破坏、合法 & 符号正确转义。全部离线、无 GITHUB_TOKEN/
网络依赖，不标 network marker，纳入 ci.yml 的 offline-tests 门禁，并在两个 deploy
workflow 的 Deploy 步骤前作为阻断门禁运行。

运行（项目根目录）：
    python3 -m pytest tests/test_xss_escape.py -q
"""

from __future__ import annotations

import os
import sys

# 让 scripts/ 可导入；T04 已将 import 守卫改为 __name__ == "__main__"，
# 因此缺 GITHUB_TOKEN 也能离线 import，无需真实 token 或网络。
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))

import deploy_pages  # noqa: E402


def test_md2html_escapes_script_payload() -> None:
    """模型输出中的 <script> 必须被转义为 &lt;script&gt;，不可执行。"""
    md = "# 报告\n\n<script>alert(1)</script>\n"
    out = deploy_pages.md2html(md)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    # 文本本体保留（仅被转义，不会被执行）。
    assert "alert(1)" in out


def test_md2html_escapes_img_onerror_payload() -> None:
    """模型输出中的 <img onerror=...> 必须被转义，不可触发 onerror。"""
    md = "- <img src=x onerror=alert(1)>\n"
    out = deploy_pages.md2html(md)
    assert "<img" not in out
    assert "&lt;img" in out
    assert "onerror=alert(1)" in out


def test_md2html_preserves_legal_bold() -> None:
    """合法 **bold** 排版必须渲染为 <strong>，不被转义破坏。"""
    md = "**加粗文本** 正常显示\n"
    out = deploy_pages.md2html(md)
    assert "<strong>加粗文本</strong>" in out
    assert "**加粗文本**" not in out


def test_md2html_escape_before_bold_replacement_order() -> None:
    """顺序正确性：先 escape 原始文本，再做 ** -> <strong> 替换。

    含 ** 的脚本标签应整体被转义（标签不可执行），内部 ** 仍会被替换，但外层
    <script> 已转义为 &lt;script&gt;，浏览器不会执行。证明 T04 的转义顺序生效。
    """
    md = "<script>**x**</script>\n"
    out = deploy_pages.md2html(md)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    # 内部 **x** 仍被替换为 <strong>（先 escape 后替换的顺序生效，不破坏合法排版）。
    assert "<strong>x</strong>" in out


def test_md2html_escapes_ampersand() -> None:
    """& 符号必须被转义为 &amp;（不会被二次转义）。"""
    md = "A & B 公司\n"
    out = deploy_pages.md2html(md)
    assert "A &amp; B" in out


def test_build_report_html_escapes_title_and_body() -> None:
    """build_report_html 的 title 与 body 均须转义模型注入文本。"""
    md = "# 报告\n\n<script>alert(1)</script>\n"
    title = "<script>t</script>"
    html = deploy_pages.build_report_html(md, title, "2026-07-27 18:00")
    # title 转义
    assert "<title><script>t</script></title>" not in html
    assert "<title>&lt;script&gt;t&lt;/script&gt;</title>" in html
    # body 转义
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_build_report_html_preserves_legal_bold() -> None:
    """build_report_html 保留合法 markdown 排版，且常量标题不被误伤。"""
    md = "**重点** 内容\n"
    html = deploy_pages.build_report_html(md, "正常标题", "2026-07-27 18:00")
    assert "<strong>重点</strong>" in html
    assert "<title>正常标题</title>" in html
