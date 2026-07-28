# -*- coding: utf-8 -*-
"""测试 deploy_pages.py 的 dist-push 纯部署逻辑 —— U2 方案C（纯 push dist）。

验证：
  * API 仍以 '/daily_stock_analysis_test/contents' 结尾、BRANCH == 'gh-pages'（常量未动）
  * _walk_dist 用 '/' 分隔的仓库相对路径遍历 dist/
  * _push_tree 遍历 dist/ 逐个上传（gh_put），并在上传前取 sha（gh_get_sha）
  * _cleanup_stale 清理陈旧文件但保留 .nojekyll 与 reports-index.json，dist 内文件不删
  * _build_reports_index / _merge_reports_index 维护归档索引（幂等去重）

全部离线：用 monkeypatch 替换 gh_put / gh_get_sha / gh_list_files / gh_delete，不发网络请求。
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))

import deploy_pages as dp  # noqa: E402


def test_api_branch_constants_preserved():
    """部署目标常量必须保持不变（红线，防回归 test_deploy_target 同义守护）。"""
    assert dp.API.endswith('/daily_stock_analysis_test/contents'), dp.API
    assert dp.BRANCH == 'gh-pages', dp.BRANCH


def test_walk_dist_uses_forward_slashes(tmp_path):
    """_walk_dist 返回 '/' 分隔的仓库相对路径（含嵌套目录）。"""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("x", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("y", encoding="utf-8")
    (dist / ".nojekyll").write_text("", encoding="utf-8")

    paths = dp._walk_dist(str(dist))
    assert "index.html" in paths
    assert "assets/app.js" in paths
    assert ".nojekyll" in paths
    assert all("\\" not in p for p in paths), "路径应使用 '/' 分隔"


def test_push_tree_uploads_every_dist_file(monkeypatch, tmp_path):
    """_push_tree 遍历 dist/ 逐个上传，内容与原文件一致，且先取 sha。"""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (dist / ".nojekyll").write_text("", encoding="utf-8")

    calls = []

    def fake_get_sha(path):
        calls.append(("get_sha", path))
        return None

    def fake_put(path, content, sha=None):
        calls.append(("put", path, content))
        return 200

    monkeypatch.setattr(dp, "gh_get_sha", fake_get_sha)
    monkeypatch.setattr(dp, "gh_put", fake_put)

    dp._push_tree(str(dist))

    put_calls = [c for c in calls if c[0] == "put"]
    put_paths = sorted(c[1] for c in put_calls)
    assert put_paths == [".nojekyll", "assets/app.js", "index.html"]

    for _, path, content in put_calls:
        assert content == (dist / path).read_text(encoding="utf-8"), f"上传内容应与 {path} 一致"

    # 每个文件上传前都取过 sha
    get_sha_paths = [c[1] for c in calls if c[0] == "get_sha"]
    assert sorted(get_sha_paths) == put_paths


def test_cleanup_stale_preserves_nojekyll_dist_and_index(monkeypatch, tmp_path):
    """_cleanup_stale 删除陈旧文件，但保留 .nojekyll、reports-index.json 与 dist 内文件。"""
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    for name in ["index.html", "report.html", "market_review.html", "vibe.html", "archive.html", "dashboard.html"]:
        (dist / name).write_text("<html></html>", encoding="utf-8")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("x", encoding="utf-8")

    existing = [
        {"name": ".nojekyll", "type": "file", "sha": "s1"},          # 保留（keep）
        {"name": "reports-index.json", "type": "file", "sha": "s2"},  # 保留（keep）
        {"name": "index.html", "type": "file", "sha": "s3"},          # 保留（在 dist_paths）
        {"name": "old_report_0900_20250728.html", "type": "file", "sha": "s4"},  # 陈旧 → 删
        {"name": "legacy_debug.json", "type": "file", "sha": "s5"},   # 陈旧 → 删
        {"name": "assets", "type": "dir", "sha": "s6"},               # 目录 → 跳过
    ]

    deleted = []

    def fake_list():
        return existing

    def fake_delete(path, sha):
        deleted.append((path, sha))
        return True

    monkeypatch.setattr(dp, "gh_list_files", fake_list)
    monkeypatch.setattr(dp, "gh_delete", fake_delete)

    dp._cleanup_stale(str(dist))

    deleted_names = sorted(n for n, _ in deleted)
    assert deleted_names == ["legacy_debug.json", "old_report_0900_20250728.html"]
    assert ".nojekyll" not in deleted_names
    assert "reports-index.json" not in deleted_names
    assert "index.html" not in deleted_names
    # 陈旧删除调用带正确 sha
    assert dict(deleted)["old_report_0900_20250728.html"] == "s4"
    assert dict(deleted)["legacy_debug.json"] == "s5"


def test_cleanup_stale_skips_when_dist_incomplete(monkeypatch, tmp_path):
    """dist 不完整（缺必需入口）时 _cleanup_stale 必须整体跳过，绝不误删线上文件。"""
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("x", encoding="utf-8")  # 仅 index，其余缺失

    deleted = []

    def fake_list():
        return [{"name": "old.html", "type": "file", "sha": "x"}]

    def fake_delete(path, sha):
        deleted.append(path)
        return True

    monkeypatch.setattr(dp, "gh_list_files", fake_list)
    monkeypatch.setattr(dp, "gh_delete", fake_delete)

    dp._cleanup_stale(str(dist))

    assert deleted == [], "dist 不完整时不应删除任何文件"


def test_build_reports_index_from_manifest():
    """_build_reports_index 从 manifest 抽取 date→{slots,fragments}。"""
    manifest = {
        "slots": {
            "0900": {"fragments": {"report": "report_0900_20250728.html", "vibe": None}},
            "1800": {"fragments": {
                "report": "report_1800_20250728.html",
                "market_review": "market_review_1800_20250728.html",
                "vibe": "vibe_1800_20250728.html",
            }},
        }
    }
    entries = dp._build_reports_index(manifest)
    assert "20250728" in entries
    assert set(entries["20250728"]["fragments"]) == {
        "report_0900_20250728.html", "report_1800_20250728.html",
        "market_review_1800_20250728.html", "vibe_1800_20250728.html",
    }
    assert entries["20250728"]["slots"] == ["0900", "1800"]


def test_merge_reports_index_idempotent():
    """_merge_reports_index 幂等合并，slot/fragment 不重复，dates 倒序。"""
    new = {"20250728": {
        "slots": ["0900", "1800"],
        "fragments": ["report_0900_20250728.html", "report_1800_20250728.html"],
    }}
    merged = dp._merge_reports_index({}, new)
    merged2 = dp._merge_reports_index(merged, new)  # 再合并一次，应无重复
    entry = merged2["entries"]["20250728"]
    assert entry["slots"] == ["0900", "1800"]
    assert entry["fragments"] == ["report_0900_20250728.html", "report_1800_20250728.html"]
    assert merged2["dates"] == ["20250728"]
    assert merged2["schemaVersion"] == "1.0"
