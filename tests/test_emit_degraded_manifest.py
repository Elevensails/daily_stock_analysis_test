# -*- coding: utf-8 -*-
"""U3 修改策略 — emit_degraded 终态端到端测试（P0-5 / P0-6）。

覆盖：
  - emit 桥接：违规报告经 repair（llm 不可用）→ 安全降级 → emit_degraded 发布
  - 片段 HTML：顶部横幅 + 占位条 div（占位标记唯一文案源精确匹配）
  - manifest：fragmentMeta 加法字段（degraded/removedSegments）；正常片段无 meta
  - jsonl：final_action=emit_degraded + degraded_segments 终态记录
  - 单元：_render_placeholders / _inject_degraded_banner / _assign_fragment_meta
  - 回归：BugFix（无时段段 market_review 日报映射 5 时段）行为不回退

llm 通过注入假 src.analyzer 模块模拟「模型不可用」，全程零网络。
"""
from __future__ import annotations

import html
import json
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))

import emit_frontend_artifacts as emit  # noqa: E402

from src.core.degrade import DEGRADED_PLACEHOLDER  # noqa: E402

ALL_SLOTS = ["0900", "0930", "1200", "1430", "1800"]

# 多段违规样本：仅 1 段踩红线，可安全降级（剥离比 < 50%）
DEGRADABLE_BAD = (
    "# 600036 招商银行 复盘\n\n"
    "今日收盘报 38.12 元，微跌 0.34%，成交额 18.6 亿，量能温和。\n\n"
    "建议现价买入，目标价 15.80 元，稳赚不赔。\n\n"
    "北向资金小幅净流出，主力资金观望情绪浓，短期以观望为主。\n\n"
    "> 以上分析基于公开数据，不构成投资建议。"
)

NORMAL_OK = "# 早盘分析\n\n今日微跌 0.34%，量能温和，短期观望。\n\n> 不构成投资建议。\n"


def _fake_broken_analyzer(monkeypatch) -> None:
    """注入假 src.analyzer：call_rewrite_llm 恒抛错（模拟模型不可用，零网络）。"""
    fake = types.ModuleType("src.analyzer")

    def _broken(system_prompt, user_prompt, *, model=None, temperature=0.1,
                segments=None):
        raise RuntimeError("model unavailable (offline test)")

    fake.call_rewrite_llm = _broken
    monkeypatch.setitem(sys.modules, "src.analyzer", fake)


def _run_emit(monkeypatch, tmp_path, reports: dict):
    """在临时目录端到端跑 emit.main()，返回 (manifest, frag_dir, log_path)。"""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    for name, body in reports.items():
        (reports_dir / name).write_text(body, encoding="utf-8")
    content_dir = tmp_path / "content"
    frag_dir = content_dir / "fragments"
    monkeypatch.setattr(emit, "_CONTENT_DIR", str(content_dir))
    monkeypatch.setattr(emit, "_FRAG_DIR", str(frag_dir))
    monkeypatch.setattr(emit, "_ROOT", str(tmp_path))  # logs/ 也进临时目录
    monkeypatch.setenv("REPORTS_DIR", str(reports_dir))
    assert emit.main() == 0
    manifest = json.loads((content_dir / "manifest.json").read_text(encoding="utf-8"))
    return manifest, frag_dir, tmp_path / "logs" / "judge_rejects.jsonl"


# ---------------------------------------------------------------------------
# 1. 单元：占位条 / 横幅 / fragmentMeta 赋值
# ---------------------------------------------------------------------------
class TestDegradedHelpers:
    def test_render_placeholders_wrapped_p(self):
        escaped = html.escape(DEGRADED_PLACEHOLDER)
        frag = f"<h1>标题</h1>\n<p>{escaped}</p>\n<p>正文</p>"
        out = emit._render_placeholders(frag)
        assert f"<p>{escaped}</p>" not in out
        assert f'<div class="dsa-degraded-placeholder">{escaped}</div>' in out
        assert "<p>正文</p>" in out  # 其余内容不受影响

    def test_render_placeholders_bare_text_fallback(self):
        escaped = html.escape(DEGRADED_PLACEHOLDER)
        frag = f"<li>{escaped}</li>"
        out = emit._render_placeholders(frag)
        assert 'class="dsa-degraded-placeholder"' in out

    def test_inject_banner_prepends(self):
        out = emit._inject_degraded_banner("<h1>x</h1>")
        assert out.startswith('<div class="dsa-degraded-banner">')
        assert out.endswith("<h1>x</h1>")

    def test_assign_fragment_meta_basic(self):
        slots = emit._init_slots()
        emit._assign_fragment_meta(
            slots, ["0900"], "report",
            {"degraded": True, "removedSegments": 1},
        )
        assert slots["0900"]["fragmentMeta"]["report"]["degraded"] is True
        assert "fragmentMeta" not in slots["1800"]  # 未涉及时段不写（加法字段）

    def test_assign_fragment_meta_daily_fallback_skips_filled(self):
        """daily fallback 覆盖规则与 _assign_fragment 对齐（meta 先于 fragment 写）。"""
        slots = emit._init_slots()
        # 1800 已被分时段片段占据
        emit._assign_fragment(
            slots, ["1800"], "market_review",
            "market_review_1800_20260731.html", is_daily_fallback=False,
        )
        emit._assign_fragment_meta(
            slots, ALL_SLOTS, "market_review",
            {"degraded": True, "removedSegments": 1}, is_daily_fallback=True,
        )
        assert "fragmentMeta" not in slots["1800"]  # 已填时段不打降级标
        assert slots["0900"]["fragmentMeta"]["market_review"]["degraded"] is True


# ---------------------------------------------------------------------------
# 2. 端到端：emit_degraded 发布 + manifest/jsonl 契约
# ---------------------------------------------------------------------------
class TestEmitDegradedEndToEnd:
    def test_degradable_report_published_with_badge_meta(
        self, monkeypatch, tmp_path
    ):
        _fake_broken_analyzer(monkeypatch)
        manifest, frag_dir, log = _run_emit(monkeypatch, tmp_path, {
            "report_0900_20260731.md": DEGRADABLE_BAD,
        })
        # 片段已发布
        fname = "report_0900_20260731.html"
        assert manifest["slots"]["0900"]["fragments"]["report"] == fname
        frag = (frag_dir / fname).read_text(encoding="utf-8")
        # 顶部横幅 + 占位条（P0-6 透明原则）
        assert 'class="dsa-degraded-banner"' in frag
        assert 'class="dsa-degraded-placeholder"' in frag
        assert html.escape(DEGRADED_PLACEHOLDER) in frag
        # 违规内容确实被剥离
        assert "建议现价买入" not in frag
        assert "稳赚不赔" not in frag
        # 未标红内容保留
        assert "38.12" in frag
        # manifest fragmentMeta 加法字段
        meta = manifest["slots"]["0900"]["fragmentMeta"]["report"]
        assert meta["degraded"] is True
        assert meta["removedSegments"] >= 1
        # jsonl 终态记录
        recs = [
            json.loads(x)
            for x in log.read_text(encoding="utf-8").strip().splitlines()
        ]
        final = [r for r in recs if r.get("final_action") == "emit_degraded"]
        assert final, "jsonl 必须写 final_action=emit_degraded 终态记录"
        assert final[0]["degraded_segments"], "终态记录须携带剥离段清单"
        # 降级稿另存备查
        assert (tmp_path / "logs" / "degraded" / "report_0900_20260731.md").is_file()

    def test_normal_report_has_no_fragment_meta(self, monkeypatch, tmp_path):
        """正常片段不写 fragmentMeta（前端 optional 读取，旧渲染无感）。"""
        _fake_broken_analyzer(monkeypatch)
        manifest, frag_dir, _ = _run_emit(monkeypatch, tmp_path, {
            "report_0900_20260731.md": NORMAL_OK,
        })
        assert manifest["slots"]["0900"]["fragments"]["report"] \
            == "report_0900_20260731.html"
        assert "fragmentMeta" not in manifest["slots"]["0900"]
        frag = (frag_dir / "report_0900_20260731.html").read_text(encoding="utf-8")
        assert "dsa-degraded" not in frag  # 正常片段零降级痕迹

    def test_undegradable_report_still_rejected(self, monkeypatch, tmp_path):
        """单段全违规（护栏触发不可降级）→ 仍 reject，永不发布不合格版。"""
        _fake_broken_analyzer(monkeypatch)
        single_bad = "建议现价买入，目标价 15.80 元，保收益无风险，稳赚不赔。"
        manifest, frag_dir, log = _run_emit(monkeypatch, tmp_path, {
            "report_0900_20260731.md": single_bad,
        })
        assert manifest["slots"]["0900"]["fragments"]["report"] is None
        assert not (frag_dir / "report_0900_20260731.html").exists()
        recs = [
            json.loads(x)
            for x in log.read_text(encoding="utf-8").strip().splitlines()
        ]
        assert any(r.get("final_action") == "reject" for r in recs)
        assert not any(
            r.get("final_action") == "emit_degraded" for r in recs
        )

    def test_manifest_schema_backward_compatible(self, monkeypatch, tmp_path):
        """既有 manifest 顶层字段不变（fragmentMeta 仅为 slot 内加法字段）。"""
        _fake_broken_analyzer(monkeypatch)
        manifest, _, _ = _run_emit(monkeypatch, tmp_path, {
            "report_0900_20260731.md": NORMAL_OK,
        })
        for key in ["schemaVersion", "generatedAt", "model", "site",
                    "stats", "slots", "fragmentTypes"]:
            assert key in manifest
        assert manifest["fragmentTypes"] == ["report", "market_review", "vibe"]


# ---------------------------------------------------------------------------
# 3. 回归：BugFix（无时段段 market_review 日报）不回退
# ---------------------------------------------------------------------------
class TestBugFixRegression:
    def test_daily_market_review_still_maps_all_slots(self, monkeypatch, tmp_path):
        _fake_broken_analyzer(monkeypatch)
        manifest, frag_dir, _ = _run_emit(monkeypatch, tmp_path, {
            "market_review_20260731.md": "## 大盘复盘\n\n指数收涨，情绪修复\n",
        })
        for code in ALL_SLOTS:
            assert (manifest["slots"][code]["fragments"]["market_review"]
                    == "market_review_20260731.html"), f"时段 {code} 回退"
        assert (frag_dir / "market_review_20260731.html").is_file()

    def test_degraded_daily_market_review_meta_on_all_open_slots(
        self, monkeypatch, tmp_path
    ):
        """降级的日报映射 5 时段时，fragmentMeta 同步铺到全部空位时段。"""
        _fake_broken_analyzer(monkeypatch)
        bad_daily = (
            "## 大盘复盘\n\n"
            "两市缩量整理，情绪中性，观望为主，成交额温和。\n\n"
            "建议现价买入指数基金，目标价 5.80 元，稳赚不赔。\n\n"
            "北向资金小幅净流出，短期以观望为主，不构成投资建议。"
        )
        manifest, _, _ = _run_emit(monkeypatch, tmp_path, {
            "market_review_20260731.md": bad_daily,
        })
        for code in ALL_SLOTS:
            assert (manifest["slots"][code]["fragments"]["market_review"]
                    == "market_review_20260731.html")
            meta = manifest["slots"][code]["fragmentMeta"]["market_review"]
            assert meta["degraded"] is True
