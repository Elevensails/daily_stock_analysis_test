# -*- coding: utf-8 -*-
"""测试 emit_frontend_artifacts.py —— U2 方案C 内容契约发射器（数据桥接）。

验证脚本把 reports/*.md 经 md2html（XSS 安全）渲染为：
    web/src/content/fragments/<type>_<HHMM>_<YYYYMMDD>.html
    web/src/content/manifest.json
并断言 manifest 字段合法、fragments 文件存在、内容经 html.escape（不含未转义 <script>）。

全部离线、无网络依赖；emit 的产出目录通过 monkeypatch 指向临时目录，绝不污染仓库。
"""

from __future__ import annotations

import os
import sys
import json
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))

import emit_frontend_artifacts as emit  # noqa: E402


def _write_report(reports_dir, name, body):
    path = os.path.join(reports_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def test_emit_produces_valid_manifest_and_escaped_fragments(monkeypatch, tmp_path):
    """跑 emit，断言 manifest 字段合法、fragments 存在且 XSS 安全。"""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    content_dir = tmp_path / "content"
    frag_dir = content_dir / "fragments"

    # 样例报告：report(0900) 含 <script> 载荷，market_review/vibe(1800)，外加应被跳过的 notes.md
    _write_report(str(reports_dir), "report_0900_20250728.md", (
        "# 早盘分析\n\n"
        "正常内容 **重点** 标记\n\n"
        "<script>alert('xss')</script>\n"
    ))
    _write_report(str(reports_dir), "market_review_1800_20250728.md", (
        "## 大盘复盘\n\n指数收涨，情绪修复\n"
    ))
    _write_report(str(reports_dir), "vibe_1800_20250728.md", (
        "## 量化\n\nRSI 超买，注意回撤\n"
    ))
    _write_report(str(reports_dir), "notes.md", "just a note, should be skipped\n")

    # 把产出目录重定向到临时目录，避免污染仓库 web/src/content
    monkeypatch.setattr(emit, "_CONTENT_DIR", str(content_dir))
    monkeypatch.setattr(emit, "_FRAG_DIR", str(frag_dir))
    monkeypatch.setenv("REPORTS_DIR", str(reports_dir))

    rc = emit.main()
    assert rc == 0

    # ---- manifest.json 字段合法性 ----
    manifest_path = content_dir / "manifest.json"
    assert manifest_path.is_file(), "manifest.json 未生成"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schemaVersion"] == "1.0"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", manifest["generatedAt"]), manifest["generatedAt"]
    assert manifest["fragmentTypes"] == ["report", "market_review", "vibe"]
    assert manifest["site"]["base"] == "/daily_stock_analysis_test/"

    # slots 预填充 5 个时段骨架
    assert set(manifest["slots"].keys()) == set(emit.SLOTS.keys())
    for code, slot in manifest["slots"].items():
        assert set(slot["fragments"].keys()) == set(emit.FRAGMENT_TYPES)

    # 类型→文件映射正确（nearest_slot 映射）
    assert manifest["slots"]["0900"]["fragments"]["report"] == "report_0900_20250728.html"
    assert manifest["slots"]["1800"]["fragments"]["market_review"] == "market_review_1800_20250728.html"
    assert manifest["slots"]["1800"]["fragments"]["vibe"] == "vibe_1800_20250728.html"

    # ---- fragments/*.html 存在 ----
    expected_frags = {
        "report_0900_20250728.html",
        "market_review_1800_20250728.html",
        "vibe_1800_20250728.html",
    }
    actual_frags = set(os.listdir(str(frag_dir)))
    assert expected_frags <= actual_frags

    # ---- 内容经 html.escape（不含未转义 <script>）----
    report_html = (frag_dir / "report_0900_20250728.html").read_text(encoding="utf-8")
    assert "<script>" not in report_html, "片段含未转义 <script>，XSS 风险"
    assert "&lt;script&gt;" in report_html, "片段未对 <script> 做 html.escape"
    # 转义仅作用于 < > & " '，文本本体不丢（单引号被转义为 &#x27;）
    assert "xss" in report_html and "alert(" in report_html, "转义后文本本体应保留"
    # 合法 markdown 排版保留
    assert "<strong>重点</strong>" in report_html

    # notes.md 不进入任何 fragment
    assert manifest["slots"]["0930"]["fragments"]["report"] is None
    assert manifest["slots"]["1200"]["fragments"]["report"] is None
    assert manifest["slots"]["1430"]["fragments"]["report"] is None


def test_emit_with_no_matching_reports_yields_all_null_slots(monkeypatch, tmp_path):
    """无任何匹配报告时，manifest 仍生成且所有片段为 null（前端据此渲染「待生成」）。"""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    _write_report(str(reports_dir), "README.md", "# not a report\n")

    content_dir = tmp_path / "content"
    frag_dir = content_dir / "fragments"
    monkeypatch.setattr(emit, "_CONTENT_DIR", str(content_dir))
    monkeypatch.setattr(emit, "_FRAG_DIR", str(frag_dir))
    monkeypatch.setenv("REPORTS_DIR", str(reports_dir))

    assert emit.main() == 0
    manifest = json.loads((content_dir / "manifest.json").read_text(encoding="utf-8"))
    for slot in manifest["slots"].values():
        assert all(v is None for v in slot["fragments"].values())
    # 无片段文件生成
    assert not any(f.endswith(".html") for f in os.listdir(str(frag_dir)))


def test_emit_reuses_md2html_and_nearest_slot(monkeypatch, tmp_path):
    """emit 必须复用 deploy_pages 的 md2html / nearest_slot，而非重实现。"""
    assert emit.md2html is not None and emit.nearest_slot is not None
    # nearest_slot 行为正确（0915 落 0900，1400 落 1430）
    assert emit.nearest_slot("0915") == "0900"
    assert emit.nearest_slot("1400") == "1430"
    # md2html 逃逸行为不变
    out = emit.md2html("<script>x</script>\n")
    assert "<script>" not in out and "&lt;script&gt;" in out
