# -*- coding: utf-8 -*-
"""回归测试：大盘分析（market_review）前端不显示 Bug 修复。

Bug 根因：TYPE_PATTERNS["market_review"] 旧正则要求文件名带 HHMM 时段段
（market_review_HHMM_YYYYMMDD.md），而 run_market_review 实际存盘名为
market_review_YYYYMMDD.md（无时段段），导致正则永不命中、大盘片段从不生成，
前端 5 个时段的「大盘分析」卡片始终显示「待生成」。

修复点（本文件逐一回归）：
  1. market_review 正则 HHMM 段改为可选，且分组语义不变
     （group(1)=HHMM 或 None，group(2)=日期）；
  2. _resolve_slot_targets：无时段段日报映射到全部 5 个时段，
     带时段段仍归入最近单一时段；
  3. _assign_fragment：日报 fallback 不覆盖已由分时段报告填充的时段；
  4. report / vibe 正则零改动（不误伤回归）；
  5. 端到端：日报 md 经 emit 后 manifest 中 5 个时段的 market_review
     均有片段，且与分时段报告共存时分时段优先。

全部离线、无网络依赖；产出目录 monkeypatch 到临时目录，不污染仓库。
"""

from __future__ import annotations

import os
import sys
import json

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))

import emit_frontend_artifacts as emit  # noqa: E402

ALL_SLOTS = ["0900", "0930", "1200", "1430", "1800"]


def _write_report(reports_dir, name, body):
    path = os.path.join(reports_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


# ---------------------------------------------------------------------------
# 1. 正则：market_review HHMM 段可选（核心修复点）
# ---------------------------------------------------------------------------
class TestMarketReviewPattern:
    def test_daily_filename_without_hhmm_matches(self):
        """日报命名 market_review_YYYYMMDD.md 必须命中（Bug 修复核心）。"""
        m = emit.TYPE_PATTERNS["market_review"].match("market_review_20260731.md")
        assert m is not None, "无时段段日报文件名未命中正则——Bug 未修复"
        assert m.group(1) is None
        assert m.group(2) == "20260731"

    def test_slotted_filename_with_hhmm_still_matches(self):
        """带时段段命名 market_review_HHMM_YYYYMMDD.md 仍命中且分组不变。"""
        m = emit.TYPE_PATTERNS["market_review"].match(
            "market_review_1800_20260731.md"
        )
        assert m is not None
        assert m.group(1) == "1800"
        assert m.group(2) == "20260731"

    @pytest.mark.parametrize("bad", [
        "market_review_2026073.md",        # 日期位数不足
        "market_review_180_20260731.md",   # HHMM 位数不足
        "market_review_20260731.txt",      # 非 md
        "market_review_.md",               # 无日期
        "xmarket_review_20260731.md",      # 前缀不锚定
    ])
    def test_malformed_names_do_not_match(self, bad):
        assert emit.TYPE_PATTERNS["market_review"].match(bad) is None


# ---------------------------------------------------------------------------
# 2. 正则回归：report / vibe 零改动，不误伤
# ---------------------------------------------------------------------------
class TestOtherPatternsUnchanged:
    @pytest.mark.parametrize("t,name,hmm,date", [
        ("report", "report_0900_20260731.md", "0900", "20260731"),
        ("vibe", "vibe_1800_20260731.md", "1800", "20260731"),
    ])
    def test_slotted_names_still_match(self, t, name, hmm, date):
        m = emit.TYPE_PATTERNS[t].match(name)
        assert m is not None
        assert m.group(1) == hmm
        assert m.group(2) == date

    @pytest.mark.parametrize("t,name", [
        ("report", "report_20260731.md"),  # report 不允许省略时段段
        ("vibe", "vibe_20260731.md"),      # vibe 不允许省略时段段
    ])
    def test_no_hhmm_names_must_not_match(self, t, name):
        """HHMM 可选仅放宽 market_review，report/vibe 必须保持强制时段段。"""
        assert emit.TYPE_PATTERNS[t].match(name) is None


# ---------------------------------------------------------------------------
# 3. _resolve_slot_targets：日报→全部 5 时段，分时段→最近单一时段
# ---------------------------------------------------------------------------
class TestResolveSlotTargets:
    def test_none_maps_to_all_five_slots(self):
        assert emit._resolve_slot_targets(None) == ALL_SLOTS

    @pytest.mark.parametrize("hmm,expected", [
        ("0900", ["0900"]),
        ("1800", ["1800"]),
        ("0915", ["0900"]),  # 复用 nearest_slot 就近归档
        ("1400", ["1430"]),
    ])
    def test_hhmm_maps_to_single_nearest_slot(self, hmm, expected):
        assert emit._resolve_slot_targets(hmm) == expected


# ---------------------------------------------------------------------------
# 4. _assign_fragment：日报 fallback 只补空位、不覆盖分时段片段
# ---------------------------------------------------------------------------
class TestAssignFragment:
    def test_daily_fallback_fills_empty_slots_only(self):
        slots = emit._init_slots()
        # 先由带时段段报告占据 1800
        emit._assign_fragment(
            slots, ["1800"], "market_review",
            "market_review_1800_20260731.html", is_daily_fallback=False,
        )
        # 日报 fallback 铺全部时段
        emit._assign_fragment(
            slots, ALL_SLOTS, "market_review",
            "market_review_20260731.html", is_daily_fallback=True,
        )
        # 1800 保持分时段片段，其余 4 个时段被日报补齐
        assert (slots["1800"]["fragments"]["market_review"]
                == "market_review_1800_20260731.html")
        for code in ["0900", "0930", "1200", "1430"]:
            assert (slots[code]["fragments"]["market_review"]
                    == "market_review_20260731.html")

    def test_non_fallback_assignment_still_overwrites(self):
        """分时段报告（非 fallback）保持原有覆盖语义。"""
        slots = emit._init_slots()
        emit._assign_fragment(
            slots, ALL_SLOTS, "market_review",
            "market_review_20260731.html", is_daily_fallback=True,
        )
        emit._assign_fragment(
            slots, ["1800"], "market_review",
            "market_review_1800_20260731.html", is_daily_fallback=False,
        )
        assert (slots["1800"]["fragments"]["market_review"]
                == "market_review_1800_20260731.html")

    def test_daily_fallback_does_not_touch_other_fragment_types(self):
        slots = emit._init_slots()
        emit._assign_fragment(
            slots, ALL_SLOTS, "market_review",
            "market_review_20260731.html", is_daily_fallback=True,
        )
        for code in ALL_SLOTS:
            assert slots[code]["fragments"]["report"] is None
            assert slots[code]["fragments"]["vibe"] is None


# ---------------------------------------------------------------------------
# 5. 端到端：日报 md 经 emit 后 manifest 中 5 时段大盘片段全部就位
# ---------------------------------------------------------------------------
class TestEndToEndDailyMarketReview:
    def _run_emit(self, monkeypatch, tmp_path, reports):
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        for name, body in reports.items():
            _write_report(str(reports_dir), name, body)
        content_dir = tmp_path / "content"
        frag_dir = content_dir / "fragments"
        monkeypatch.setattr(emit, "_CONTENT_DIR", str(content_dir))
        monkeypatch.setattr(emit, "_FRAG_DIR", str(frag_dir))
        monkeypatch.setenv("REPORTS_DIR", str(reports_dir))
        assert emit.main() == 0
        manifest = json.loads(
            (content_dir / "manifest.json").read_text(encoding="utf-8")
        )
        return manifest, frag_dir

    def test_daily_report_fills_all_five_slots(self, monkeypatch, tmp_path):
        """仅一份无时段段日报 → 5 个时段的大盘卡片全部有内容（不再「待生成」）。"""
        manifest, frag_dir = self._run_emit(monkeypatch, tmp_path, {
            "market_review_20260731.md": "## 大盘复盘\n\n指数收涨，情绪修复\n",
        })
        for code in ALL_SLOTS:
            assert (manifest["slots"][code]["fragments"]["market_review"]
                    == "market_review_20260731.html"), f"时段 {code} 大盘片段缺失"
        # 片段文件名无时段段（<type>_<date>.html），且确实落盘
        assert (frag_dir / "market_review_20260731.html").is_file()

    def test_slotted_report_takes_priority_over_daily(self, monkeypatch, tmp_path):
        """日报与分时段报告共存：分时段占其时段，日报只补其余空位。"""
        manifest, _ = self._run_emit(monkeypatch, tmp_path, {
            "market_review_20260731.md": "## 大盘日报\n\n全天复盘内容\n",
            "market_review_1800_20260731.md": "## 收盘大盘复盘\n\n分时段内容\n",
        })
        assert (manifest["slots"]["1800"]["fragments"]["market_review"]
                == "market_review_1800_20260731.html")
        for code in ["0900", "0930", "1200", "1430"]:
            assert (manifest["slots"][code]["fragments"]["market_review"]
                    == "market_review_20260731.html")

    def test_report_and_vibe_pipeline_unaffected(self, monkeypatch, tmp_path):
        """回归：report / vibe 处理路径与日报共存时行为不变。"""
        manifest, _ = self._run_emit(monkeypatch, tmp_path, {
            "market_review_20260731.md": "## 大盘复盘\n\n指数收涨\n",
            "report_0900_20260731.md": "# 早盘分析\n\n正常内容\n",
            "vibe_1800_20260731.md": "## 量化\n\nRSI 中性\n",
        })
        assert (manifest["slots"]["0900"]["fragments"]["report"]
                == "report_0900_20260731.html")
        assert (manifest["slots"]["1800"]["fragments"]["vibe"]
                == "vibe_1800_20260731.html")
        # report/vibe 未被日报污染：其余时段仍为 null
        assert manifest["slots"]["0930"]["fragments"]["report"] is None
        assert manifest["slots"]["0900"]["fragments"]["vibe"] is None
