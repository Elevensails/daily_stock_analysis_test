# -*- coding: utf-8 -*-
"""Focused tests for the archive.html preview feature (Task 4).

Covers:
  B.1  extract_preview() pure function (no token, no network)
  B.2  make_archive_page() integration (network fully mocked)
  B.3  make_archive_page() safe-degrade when content fetch fails

Run with (from project root daily_stock_analysis/):
    GITHUB_TOKEN=dummy \\
        <py> -m unittest tests.test_archive_preview -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

# deploy_pages.py raises SystemExit at import time if GITHUB_TOKEN is empty,
# so guarantee it is set before importing the module.
os.environ.setdefault("GITHUB_TOKEN", "dummy")

# Make scripts/ importable regardless of how the test runner is invoked.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))

import deploy_pages  # noqa: E402  (import gated on token above)


# --------------------------------------------------------------------------
# Sample fixtures
# --------------------------------------------------------------------------

# A realistic report page returned by gh_get_content(): contains a <style>,
# <script>, <footer> (disclaimer) and a div.nav (no <nav> tag, so nav residue
# must be filtered via the skip-prefix logic), plus template braces {BTC}/{high}.
SAMPLE_REPORT_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<style>.badge{color:red;background:#fff}</style></head>
<body>
<div class="nav"><a href="index.html">首页</a><span>/</span><a href="archive.html">历史归档</a></div>
<div class="module"><h1>复盘</h1><p>今日 BTC 突破 {BTC} 美元 高点 {high}</p></div>
<footer>2026-07-21 · DeepSeek AI · 以上分析基于公开数据，不构成投资建议</footer>
<script>var x=1;console.log('hi');</script>
</body></html>"""

# A sample exercising every filter branch of extract_preview() for the pure
# function test: style/script/footer/nav blocks, disclaimer lines, and
# navigation-residue lines that start with the skip prefixes.
SAMPLE_PURE_HTML = """<html><head><style>.cls{color:red;background:#fff}</style></head>
<body>
<nav><a href="home">首页</a><a href="arch">历史归档</a></nav>
<div class="module"><h2>大盘综述</h2><p>今日两市放量上行，北向资金净流入。</p></div>
<footer>以上分析基于公开数据，不构成投资建议</footer>
<script>var x=1;console.log('hi');</script>
<p>首页 / 历史归档 导航残留</p>
<p>不构成投资建议 请勿据此操作</p>
</body></html>"""


# --------------------------------------------------------------------------
# B.1  extract_preview() pure function
# --------------------------------------------------------------------------

class TestExtractPreview(unittest.TestCase):
    def test_strips_blocks_and_disclaimer(self):
        out = deploy_pages.extract_preview(SAMPLE_PURE_HTML)
        # style / script internals must be gone
        self.assertNotIn("color:red", out)
        self.assertNotIn("var x=1", out)
        # disclaimer lines dropped
        self.assertNotIn("不构成投资建议", out)
        self.assertNotIn("以上分析基于公开数据", out)
        # navigation residue dropped
        self.assertNotIn("首页", out)
        self.assertNotIn("历史归档", out)
        # length within budget (or truncated with ellipsis)
        self.assertTrue(len(out) <= 90 or "…" in out,
                        f"preview too long / not truncated: {out!r}")

    def test_keeps_braces(self):
        # extract_preview only does unescape; brace->entity replacement happens
        # later in make_archive_page, so raw braces survive here.
        out = deploy_pages.extract_preview("<p>价格 {BTC} 高点 {high}</p>")
        self.assertIn("{", out)
        self.assertIn("}", out)
        self.assertIn("{BTC}", out)
        self.assertIn("{high}", out)

    def test_long_text_truncates_with_ellipsis(self):
        long_text = "<p>" + ("市场 " * 60) + "</p>"
        out = deploy_pages.extract_preview(long_text)
        self.assertIn("…", out)
        self.assertLessEqual(len(out), 91)  # max_len 90 + ellipsis

    def test_empty_input(self):
        self.assertEqual(deploy_pages.extract_preview(""), "")
        self.assertEqual(deploy_pages.extract_preview(None), "")


# --------------------------------------------------------------------------
# B.2 / B.3  make_archive_page() integration (network mocked)
# --------------------------------------------------------------------------

class TestMakeArchivePage(unittest.TestCase):
    def _make_file_list(self):
        """Construct a fake gh-pages file listing (GitHub contents API shape)."""
        return [
            {"name": "market_review_1800_20260721.html", "path": "market_review_1800_20260721.html", "sha": "a", "size": 10, "type": "file"},
            {"name": "report_0900_20260721.html", "path": "report_0900_20260721.html", "sha": "b", "size": 10, "type": "file"},
            {"name": "vibe_1200_20260721.html", "path": "vibe_1200_20260721.html", "sha": "c", "size": 10, "type": "file"},
            {"name": "report_0900_20260720.html", "path": "report_0900_20260720.html", "sha": "d", "size": 10, "type": "file"},
            # non-report files must be ignored by the relaxed grouping regex
            {"name": "index.html", "path": "index.html", "sha": "e", "size": 10, "type": "file"},
            {"name": "slot_0900.html", "path": "slot_0900.html", "sha": "f", "size": 10, "type": "file"},
        ]

    def _run(self, get_content_return):
        """Run make_archive_page with all network calls mocked.

        Returns the generated archive.html content string captured from gh_put.
        """
        captured = {}

        def fake_put(path, content, sha=None):
            captured["path"] = path
            captured["content"] = content
            return 201

        files = self._make_file_list()
        with mock.patch.object(deploy_pages, "gh_list_files", return_value=files), \
             mock.patch.object(deploy_pages, "gh_get_content", return_value=get_content_return), \
             mock.patch.object(deploy_pages, "gh_get_sha", return_value=None), \
             mock.patch.object(deploy_pages, "gh_put", side_effect=fake_put):
            result = deploy_pages.make_archive_page()

        self.assertEqual(result, 201, "gh_put should report a successful deploy")
        self.assertEqual(captured["path"], "archive.html")
        return captured["content"]

    def test_integration_links_preview_and_escapes_braces(self):
        content = self._run(SAMPLE_REPORT_HTML)

        # 1) href points to the real representative file, NOT slot_0900.html
        self.assertIn('href="market_review_1800_20260721.html"', content)
        self.assertNotIn('href="slot_0900.html"', content)

        # 2) preview snippet text appears
        self.assertIn("BTC", content)
        # the preview div exists and carries the snippet
        self.assertIn('<div class="preview">', content)

        # 3) braces in the preview are escaped so the f-string is safe
        self.assertIn("&#123;BTC&#125;", content)
        self.assertIn("&#123;high&#125;", content)
        # extracted preview div must not leak raw nav/disclaimer/script residue
        import re
        m = re.search(r'<div class="preview">(.*?)</div>', content, re.DOTALL)
        self.assertIsNotNone(m, "preview div should be present")
        inner = m.group(1)
        self.assertNotIn("首页", inner)
        self.assertNotIn("不构成投资建议", inner)
        self.assertNotIn("var x=1", inner)

        # 4) per-day counts reflect ALL report types for that day
        self.assertIn("3 份报告", content)   # 20260721: report+market_review+vibe
        self.assertIn("1 份报告", content)   # 20260720: report only

    def test_safe_degrade_when_content_unavailable(self):
        # gh_get_content returns None for every file -> no preview, no error.
        content = self._run(None)

        self.assertNotIn('<div class="preview">', content)
        # dates and counts still render
        self.assertIn("3 份报告", content)
        self.assertIn("1 份报告", content)
        # href still points to the real representative file
        self.assertIn('href="market_review_1800_20260721.html"', content)
        self.assertNotIn('href="slot_0900.html"', content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
