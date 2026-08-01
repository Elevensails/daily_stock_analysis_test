# -*- coding: utf-8 -*-
"""P1.6 T04/T05 — RAG 新闻 akshare 源与多源降级拓扑的独立 QA 验证。

对应 docs/p1.6-arch-design.md §3.5 / §3.6 / §3.7 / §7.1，
PRD P0-2 / P0-3 / P0-4 / P1-1，以及 DoD 第 4 项（源码无硬编码路径）。

本文件由 QA 独立补充（设计 §2.1 规划了 tests/test_rag_news_akshare.py
与 tests/test_rag_news_integration.py，工程师交付时未创建，仅在
tests/test_rag_integration.py 中覆盖了 3 条）。

全部 mock akshare 与 subprocess，零网络（§7.5 / C4）。
"""

import re
import sys
import threading
import time
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.rag import news as news_mod
from src.rag._akshare_client import (
    fetch_stock_news,
    is_circuit_open,
    reset_circuit,
)
from src.rag.news import (
    SOURCE_AKSHARE_EM,
    SOURCE_NEODATA,
    SOURCE_NONE,
    SOURCE_SEARCH_SERVICE,
    retrieve_news,
    retrieve_news_ex,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# 真实实现的引用。模块级 autouse 夹具 `_no_neodata` 会把它替换成永远返回 None
# 的桩，TestNeodataPathResolution 专测三级解析本体，必须先拿回真身，
# 否则该类的断言会全部落在桩上（两条期望路径的用例直接红，
# 两条期望 None 的用例则是「假绿」）。
_REAL_RESOLVE_NEODATA_SCRIPT = news_mod._resolve_neodata_script


# ─────────────────────────────────────────────────────────────
# 测试替身
# ─────────────────────────────────────────────────────────────
class FakeDataFrame:
    """最小 DataFrame 替身：只需 .empty 与 .to_dict('records')。"""

    def __init__(self, records):
        self._records = list(records)

    @property
    def empty(self):
        return not self._records

    def to_dict(self, orient="records"):
        assert orient == "records"
        return list(self._records)


def _news_record(title, content="正文内容", published=None, origin="证券时报", url="http://x"):
    published = published or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "关键词": "600036",
        "新闻标题": title,
        "新闻内容": content,
        "发布时间": published,
        "文章来源": origin,
        "新闻链接": url,
    }


def _cfg(**overrides):
    """构造 config 替身，默认值贴合 config.py 声明。"""
    base = dict(
        rag_neodata_script_path="",
        rag_neodata_timeout_seconds=8.0,
        rag_akshare_news_enabled=True,
        rag_akshare_news_timeout_seconds=6.0,
        rag_akshare_news_max_items=8,
        rag_akshare_news_lookback_days=7,
        rag_akshare_news_max_retries=1,
        rag_news_total_budget_seconds=20.0,
        rag_news_merge_sources=False,
        rag_news_min_items=1,
        rag_news_dedup_title_chars=20,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _clean_circuit():
    reset_circuit()
    yield
    reset_circuit()


@pytest.fixture(autouse=True)
def _no_neodata(monkeypatch):
    """默认屏蔽 neodata（模拟 CI 环境无脚本）。需要时用例内再放开。"""
    monkeypatch.setattr(news_mod, "_resolve_neodata_script", lambda config=None: None)
    yield


@pytest.fixture(autouse=True)
def _fast_rate_limit(monkeypatch):
    """去掉模块级 0.8s 限频，避免拖慢测试。"""
    monkeypatch.setattr("src.rag._akshare_client._MIN_INTERVAL_SECONDS", 0.0)
    yield


# ═════════════════════════════════════════════════════════════
# 1. DoD 第 4 项 + §7.1 分层守卫（静态源码断言）
# ═════════════════════════════════════════════════════════════
class TestSourceCodeGuards:
    """硬性验收：源码不得含开发者硬编码路径；rag 新闻侧不得跨层。"""

    P16_RAG_FILES = ["src/rag/news.py", "src/rag/_akshare_client.py"]

    @pytest.mark.parametrize("relative", P16_RAG_FILES)
    def test_no_hardcoded_developer_path(self, relative):
        """DoD 第 4 项：源码中不存在 C:/Users/29096 硬编码字面量。"""
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "C:/Users/29096" not in text
        assert "C:\\Users\\29096" not in text

    def test_no_hardcoded_path_anywhere_in_src(self):
        """全 src/ 与 data_provider/ 范围的兜底扫描。"""
        offenders = []
        for base in ("src", "data_provider", "scripts"):
            for path in (REPO_ROOT / base).rglob("*.py"):
                text = path.read_text(encoding="utf-8", errors="replace")
                if "C:/Users/29096" in text or "C:\\Users\\29096" in text:
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == [], f"发现硬编码开发者路径: {offenders}"

    @pytest.mark.parametrize("relative", P16_RAG_FILES)
    def test_no_cross_layer_dependency_on_data_provider(self, relative):
        """§7.1：news.py / _akshare_client.py 禁止 import data_provider。

        只禁「真实 import 语句」，不禁注释/docstring 里说明「为什么不复用
        data_provider」的文字提及 —— 后者恰恰是架构决策 Q7 明确要求写进源码的
        依据（`_akshare_client.py` 开头就在解释这件事），用裸子串断言会误红。
        正则允许前导空白，因此函数内的惰性 import 同样会被抓到。

        注意守卫范围不含 financial.py（既有正当复用，扩大范围会误红，
        见 docs/p1.6-arch-design.md:1214）。
        """
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in text.splitlines()
            if re.match(r"^\s*(?:import|from)\s+", line) and "data_provider" in line
        ]
        assert offenders == [], f"{relative} 出现跨层 import: {offenders}"

    def test_akshare_client_wraps_call_with_thread_pool_timeout(self):
        """E1：ak.stock_news_em 无 timeout 参数，必须用线程池 result(timeout=) 截断。"""
        text = (REPO_ROOT / "src/rag/_akshare_client.py").read_text(encoding="utf-8")
        assert "ThreadPoolExecutor" in text
        assert re.search(r"\.result\(\s*timeout\s*=", text), "必须用 result(timeout=) 强制截断"
        assert "shutdown(wait=False)" in text, "超时后必须非阻塞丢弃执行器，避免 hang 住 CI"


# ═════════════════════════════════════════════════════════════
# 2. _akshare_client：超时 / 熔断 / 异常吞噬（P0-4、E1）
# ═════════════════════════════════════════════════════════════
class TestAkshareClient:
    """§3.5 极简客户端的四态：成功 / 空 / 超时 / 异常。"""

    @staticmethod
    def _install_fake_ak(monkeypatch, func):
        fake = types.ModuleType("akshare")
        fake.stock_news_em = func
        monkeypatch.setitem(sys.modules, "akshare", fake)
        return fake

    def test_success_returns_records(self, monkeypatch):
        self._install_fake_ak(
            monkeypatch, lambda symbol: FakeDataFrame([_news_record("利好公告")])
        )
        records = fetch_stock_news("600036", timeout=5.0)
        assert len(records) == 1
        assert records[0]["新闻标题"] == "利好公告"

    def test_empty_dataframe_returns_empty_and_is_not_a_failure(self, monkeypatch):
        """冷门票没新闻不是故障，不得累计熔断。"""
        self._install_fake_ak(monkeypatch, lambda symbol: FakeDataFrame([]))
        for _ in range(5):
            assert fetch_stock_news("600036", timeout=5.0) == []
        assert is_circuit_open() is False

    def test_timeout_is_truncated_and_never_raises(self, monkeypatch):
        """E1 核心：底层调用挂起时必须被线程池截断，且不抛异常。"""
        started = threading.Event()

        def _hang(symbol):
            started.set()
            time.sleep(30)  # 模拟挂起的 requests
            return FakeDataFrame([])

        self._install_fake_ak(monkeypatch, _hang)

        t0 = time.time()
        records = fetch_stock_news("600036", timeout=1.0)
        elapsed = time.time() - t0

        assert records == [], "超时必须静默返回空列表（P0-4）"
        assert started.is_set()
        assert elapsed < 5.0, f"超时未被截断，实际耗时 {elapsed:.1f}s"

    def test_timeout_does_not_block_next_call(self, monkeypatch):
        """超时后丢弃执行器，下一只票不得被僵尸线程堵死。"""
        calls = {"n": 0}

        def _first_hangs(symbol):
            calls["n"] += 1
            if calls["n"] == 1:
                time.sleep(30)
            return FakeDataFrame([_news_record("第二次成功")])

        self._install_fake_ak(monkeypatch, _first_hangs)

        assert fetch_stock_news("600036", timeout=1.0) == []
        t0 = time.time()
        records = fetch_stock_news("603823", timeout=5.0)
        assert time.time() - t0 < 5.0, "上一次超时的线程堵住了后续调用"
        assert len(records) == 1

    def test_exception_is_swallowed(self, monkeypatch):
        def _boom(symbol):
            raise RuntimeError("east money down")

        self._install_fake_ak(monkeypatch, _boom)
        assert fetch_stock_news("600036", timeout=5.0) == []

    def test_circuit_opens_after_consecutive_failures(self, monkeypatch):
        """连续 3 次失败 → 进程内熔断，后续直接跳过。"""
        calls = {"n": 0}

        def _boom(symbol):
            calls["n"] += 1
            raise RuntimeError("down")

        self._install_fake_ak(monkeypatch, _boom)

        for _ in range(3):
            fetch_stock_news("600036", timeout=1.0)
        assert is_circuit_open() is True

        before = calls["n"]
        assert fetch_stock_news("600036", timeout=1.0) == []
        assert calls["n"] == before, "熔断后不应再真正发起调用"

    def test_success_resets_circuit(self, monkeypatch):
        state = {"fail": True}

        def _flaky(symbol):
            if state["fail"]:
                raise RuntimeError("down")
            return FakeDataFrame([_news_record("恢复")])

        self._install_fake_ak(monkeypatch, _flaky)
        for _ in range(2):
            fetch_stock_news("600036", timeout=1.0)
        state["fail"] = False
        fetch_stock_news("600036", timeout=1.0)
        assert is_circuit_open() is False

    def test_missing_akshare_module_is_handled(self, monkeypatch):
        """akshare 未安装时不得抛出（CI 零新增依赖假设失效的兜底）。"""
        monkeypatch.setitem(sys.modules, "akshare", None)
        assert fetch_stock_news("600036", timeout=1.0) == []

    def test_empty_symbol_short_circuits(self):
        assert fetch_stock_news("", timeout=1.0) == []


# ═════════════════════════════════════════════════════════════
# 3. _akshare_news：字段映射与时间窗（§3.6 Q5）
# ═════════════════════════════════════════════════════════════
class TestAkshareNewsMapping:
    def test_field_mapping(self, monkeypatch):
        record = _news_record("招行发布年报", content="净利润同比增长", origin="证券时报")
        monkeypatch.setattr(news_mod, "fetch_stock_news", lambda s, timeout=0: [record])

        items = news_mod._akshare_news("600036", config=_cfg())

        assert len(items) == 1
        item = items[0]
        assert item["title"] == "招行发布年报"
        assert item["summary"] == "净利润同比增长"
        assert item["source"] == SOURCE_AKSHARE_EM, "内部源标识必须固定为 akshare_em"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["date"])

    def test_titles_and_summaries_are_truncated(self, monkeypatch):
        record = _news_record("标" * 200, content="正" * 500)
        monkeypatch.setattr(news_mod, "fetch_stock_news", lambda s, timeout=0: [record])

        item = news_mod._akshare_news("600036", config=_cfg())[0]
        assert len(item["title"]) <= 80
        assert len(item["summary"]) <= 200

    def test_empty_title_records_are_dropped(self, monkeypatch):
        records = [_news_record(""), _news_record("   "), _news_record("有效标题")]
        monkeypatch.setattr(news_mod, "fetch_stock_news", lambda s, timeout=0: records)

        items = news_mod._akshare_news("600036", config=_cfg())
        assert [i["title"] for i in items] == ["有效标题"]

    def test_old_news_filtered_by_lookback_window(self, monkeypatch):
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        records = [_news_record("陈年旧闻", published=old), _news_record("今日新闻")]
        monkeypatch.setattr(news_mod, "fetch_stock_news", lambda s, timeout=0: records)

        items = news_mod._akshare_news("600036", config=_cfg(rag_akshare_news_lookback_days=7))
        assert [i["title"] for i in items] == ["今日新闻"]

    def test_unparseable_date_is_kept(self, monkeypatch):
        """§3.6：空/异常日期保守保留（宁可多不可少）。"""
        records = [_news_record("无日期", published="不是日期")]
        monkeypatch.setattr(news_mod, "fetch_stock_news", lambda s, timeout=0: records)

        items = news_mod._akshare_news("600036", config=_cfg())
        assert len(items) == 1
        assert items[0]["date"] == ""

    def test_max_items_is_respected(self, monkeypatch):
        records = [_news_record(f"新闻{i}") for i in range(50)]
        monkeypatch.setattr(news_mod, "fetch_stock_news", lambda s, timeout=0: records)

        items = news_mod._akshare_news("600036", config=_cfg(rag_akshare_news_max_items=8))
        assert len(items) == 8

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("600036", "600036"),
            ("sh600036", "600036"),
            ("600036.SH", "600036"),
            ("SH.600036", "600036"),
        ],
    )
    def test_code_normalization(self, raw, expected):
        assert news_mod._normalize_news_symbol(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "ABC", "12345"])
    def test_uncoercible_code_skips_source(self, raw, monkeypatch):
        called = {"n": 0}

        def _spy(s, timeout=0):
            called["n"] += 1
            return []

        monkeypatch.setattr(news_mod, "fetch_stock_news", _spy)
        assert news_mod._akshare_news(raw, config=_cfg()) == []
        assert called["n"] == 0, "无法归一的代码不应发起调用"

    def test_e2_date_parse_handles_T_separator(self):
        """E2 边界：部分记录用 T 分隔。"""
        assert news_mod._parse_news_date("2026-02-13T09:30:00") == "2026-02-13"
        assert news_mod._parse_news_date("2026-02-13 09:30:00") == "2026-02-13"
        assert news_mod._parse_news_date("2026/02/13 09:30") == "2026-02-13"
        assert news_mod._parse_news_date("") == ""
        assert news_mod._parse_news_date(None) == ""


# ═════════════════════════════════════════════════════════════
# 4. 串行短路拓扑（§3.6 Q6、P0-2、P0-3）
# ═════════════════════════════════════════════════════════════
class TestSerialFallbackTopology:
    """neodata → akshare_em → search_service → 空 block。"""

    @staticmethod
    def _patch_sources(monkeypatch, neodata=None, akshare=None, search=None):
        calls = []

        def _mk(name, payload):
            def _fn(*args, **kwargs):
                calls.append(name)
                return list(payload or [])

            return _fn

        monkeypatch.setattr(news_mod, "_neodata_news", _mk("neodata", neodata))
        monkeypatch.setattr(news_mod, "_akshare_news", _mk("akshare", akshare))
        monkeypatch.setattr(news_mod, "_search_service_fallback", _mk("search", search))
        return calls

    @staticmethod
    def _items(n, prefix="标题"):
        return [
            {"title": f"{prefix}{i}", "summary": "s", "date": "2026-02-13", "source": "x"}
            for i in range(n)
        ]

    def test_neodata_hit_short_circuits_remaining_sources(self, monkeypatch):
        """P0-3：本地有 neodata 时首选，且不再调用后续源。"""
        calls = self._patch_sources(monkeypatch, neodata=self._items(3))

        result = retrieve_news_ex("600036", "招商银行", config=_cfg())

        assert calls == ["neodata"], f"应短路，实际调用链: {calls}"
        assert result.hit_source == SOURCE_NEODATA
        assert result.success is True

    def test_falls_through_to_akshare_when_neodata_empty(self, monkeypatch):
        """P0-2：CI 无 neodata 时降级到 akshare。"""
        calls = self._patch_sources(monkeypatch, neodata=[], akshare=self._items(3))

        result = retrieve_news_ex("600036", "招商银行", config=_cfg())

        assert calls == ["neodata", "akshare"]
        assert result.hit_source == SOURCE_AKSHARE_EM
        assert "### 近期动态" in result.block

    def test_falls_through_to_search_service_last(self, monkeypatch):
        calls = self._patch_sources(
            monkeypatch, neodata=[], akshare=[], search=self._items(2)
        )

        result = retrieve_news_ex("600036", "招商银行", config=_cfg())

        assert calls == ["neodata", "akshare", "search"]
        assert result.hit_source == SOURCE_SEARCH_SERVICE

    def test_all_sources_empty_yields_empty_block(self, monkeypatch):
        calls = self._patch_sources(monkeypatch, neodata=[], akshare=[], search=[])

        result = retrieve_news_ex("600036", "招商银行", config=_cfg())

        assert calls == ["neodata", "akshare", "search"]
        assert result.block == ""
        assert result.hit_source == SOURCE_NONE
        assert result.success is False

    def test_merge_sources_true_calls_all_sources(self, monkeypatch):
        """§3.6：merge_sources=True 恢复全源合并行为（保留退路）。"""
        calls = self._patch_sources(
            monkeypatch,
            neodata=self._items(2, "N"),
            akshare=self._items(2, "A"),
            search=self._items(2, "S"),
        )

        result = retrieve_news_ex(
            "600036", "招商银行", config=_cfg(rag_news_merge_sources=True)
        )

        assert calls == ["neodata", "akshare", "search"], "合并模式必须遍历全部源"
        assert result.hit_source == SOURCE_NEODATA
        assert result.counts == {"neodata": 2, "akshare_em": 2, "search_service": 2}

    def test_akshare_disabled_by_config_is_skipped(self, monkeypatch):
        """P1-4：开关必须真正生效。"""
        calls = self._patch_sources(monkeypatch, neodata=[], akshare=self._items(5), search=[])

        result = retrieve_news_ex(
            "600036", "招商银行", config=_cfg(rag_akshare_news_enabled=False)
        )

        assert "akshare" not in calls
        assert result.hit_source == SOURCE_NONE

    def test_min_items_threshold_controls_short_circuit(self, monkeypatch):
        """命中数 < min_items 时应继续降级。"""
        calls = self._patch_sources(
            monkeypatch, neodata=self._items(1, "N"), akshare=self._items(5, "A")
        )

        retrieve_news_ex("600036", "招商银行", config=_cfg(rag_news_min_items=3))

        assert calls == ["neodata", "akshare"], "未达阈值应继续尝试下一源"

    def test_source_exception_never_propagates(self, monkeypatch):
        """P0-4：任一源抛异常都必须被吞掉并继续降级。"""

        def _boom(*args, **kwargs):
            raise RuntimeError("source exploded")

        monkeypatch.setattr(news_mod, "_neodata_news", _boom)
        monkeypatch.setattr(news_mod, "_akshare_news", _boom)
        monkeypatch.setattr(news_mod, "_search_service_fallback", _boom)

        result = retrieve_news_ex("600036", "招商银行", config=_cfg())

        assert result.block == ""
        assert result.hit_source == SOURCE_NONE
        assert result.attempted == [SOURCE_NEODATA, SOURCE_AKSHARE_EM, SOURCE_SEARCH_SERVICE]

    def test_budget_exhausted_skips_later_sources(self, monkeypatch):
        """Q8：deadline 强制截断。"""

        def _slow(*args, **kwargs):
            time.sleep(0.3)
            return []

        monkeypatch.setattr(news_mod, "_neodata_news", _slow)
        akshare_called = {"n": 0}

        def _ak(*args, **kwargs):
            akshare_called["n"] += 1
            return []

        monkeypatch.setattr(news_mod, "_akshare_news", _ak)
        monkeypatch.setattr(news_mod, "_search_service_fallback", lambda *a, **k: [])

        result = retrieve_news_ex(
            "600036", "招商银行", config=_cfg(rag_news_total_budget_seconds=0.1)
        )

        assert result.budget_exceeded is True
        assert akshare_called["n"] == 0

    def test_trace_counts_and_elapsed_are_populated(self, monkeypatch):
        self._patch_sources(monkeypatch, neodata=[], akshare=self._items(4))

        result = retrieve_news_ex("600036", "招商银行", config=_cfg())

        assert result.counts["neodata"] == 0
        assert result.counts["akshare_em"] == 4
        assert result.elapsed_ms >= 0
        assert result.to_dict()["hit_source"] == SOURCE_AKSHARE_EM

    def test_retrieve_news_shim_returns_block_string(self, monkeypatch):
        """§7.6 红线：retrieve_news 仍返回 str。"""
        self._patch_sources(monkeypatch, neodata=[], akshare=self._items(3))

        block = retrieve_news("600036", "招商银行", config=_cfg())

        assert isinstance(block, str)
        assert "### 近期动态" in block

    def test_retrieve_news_ex_never_raises_without_config(self, monkeypatch):
        self._patch_sources(monkeypatch, neodata=[], akshare=[], search=[])
        result = retrieve_news_ex("600036", "招商银行")
        assert result.hit_source == SOURCE_NONE


# ═════════════════════════════════════════════════════════════
# 5. neodata 路径解析（P1-1 / §3.7）
# ═════════════════════════════════════════════════════════════
class TestNeodataPathResolution:
    @pytest.fixture(autouse=True)
    def _restore_real_resolver(self, monkeypatch):
        """撤销模块级 `_no_neodata` 的桩，本类要测 `_resolve_neodata_script` 本体。

        类级 autouse 夹具在模块级 autouse 夹具之后执行，因此这里的 setattr
        会覆盖掉桩；monkeypatch 在用例结束后自动还原，不影响其它测试类。
        """
        monkeypatch.setattr(
            news_mod, "_resolve_neodata_script", _REAL_RESOLVE_NEODATA_SCRIPT
        )
        yield

    def test_returns_none_when_nothing_configured(self, monkeypatch, tmp_path):
        monkeypatch.delenv("RAG_NEODATA_SCRIPT_PATH", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        assert news_mod._resolve_neodata_script(_cfg()) is None

    def test_explicit_config_path_wins(self, monkeypatch, tmp_path):
        script = tmp_path / "query.py"
        script.write_text("# stub", encoding="utf-8")
        monkeypatch.delenv("RAG_NEODATA_SCRIPT_PATH", raising=False)

        resolved = news_mod._resolve_neodata_script(
            _cfg(rag_neodata_script_path=str(script))
        )
        assert resolved == script

    def test_env_var_used_when_config_absent(self, monkeypatch, tmp_path):
        script = tmp_path / "query.py"
        script.write_text("# stub", encoding="utf-8")
        monkeypatch.setenv("RAG_NEODATA_SCRIPT_PATH", str(script))

        assert news_mod._resolve_neodata_script(_cfg()) == script

    def test_nonexistent_configured_path_degrades_silently(self, monkeypatch, tmp_path):
        monkeypatch.delenv("RAG_NEODATA_SCRIPT_PATH", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        assert (
            news_mod._resolve_neodata_script(
                _cfg(rag_neodata_script_path=str(tmp_path / "nope.py"))
            )
            is None
        )

    def test_neodata_news_returns_empty_without_script(self, monkeypatch):
        """CI 场景：脚本不存在 → 立即返回 []，不得调用 subprocess。"""
        monkeypatch.setattr(news_mod, "_resolve_neodata_script", lambda config=None: None)
        with patch("subprocess.run") as mock_run:
            assert news_mod._neodata_news("600036", "招商银行", config=_cfg()) == []
        mock_run.assert_not_called()

    def test_neodata_subprocess_timeout_is_handled(self, monkeypatch, tmp_path):
        import subprocess

        script = tmp_path / "query.py"
        script.write_text("# stub", encoding="utf-8")
        monkeypatch.setattr(news_mod, "_resolve_neodata_script", lambda config=None: script)

        with patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=8)
        ):
            assert news_mod._neodata_news("600036", "招商银行", config=_cfg()) == []


# ═════════════════════════════════════════════════════════════
# 6. T05 — retriever 的 SourceTrace 精确化
# ═════════════════════════════════════════════════════════════
class TestRetrieverSourceTrace:
    """§4.2 收口：actual_source 用 hit_source，不再靠子串猜测。"""

    def test_retriever_uses_no_substring_guessing(self):
        text = (REPO_ROOT / "src/rag/retriever.py").read_text(encoding="utf-8")
        assert '"search_service" not in news_text' not in text
        assert "news_result.hit_source" in text

    @pytest.mark.parametrize(
        "hit_source", [SOURCE_NEODATA, SOURCE_AKSHARE_EM, SOURCE_SEARCH_SERVICE]
    )
    def test_actual_source_matches_hit_source(self, monkeypatch, hit_source):
        from src.rag import retriever as retriever_mod
        from src.rag.news import NewsRetrievalResult

        fake = NewsRetrievalResult(
            block="### 近期动态\n- 2026-02-13 标题",
            hit_source=hit_source,
            elapsed_ms=12.0,
        )
        monkeypatch.setattr("src.rag.news.retrieve_news_ex", lambda *a, **k: fake)
        monkeypatch.setattr(retriever_mod, "retrieve_financial", lambda *a, **k: "", raising=False)

        ctx = retriever_mod.retrieve_financial_context("600036", "招商银行")
        news_trace = [t for t in ctx.source_trace if t.dimension == "news"][0]

        assert news_trace.actual_source == hit_source
        assert news_trace.success is True

    def test_news_body_containing_source_name_does_not_mislead_trace(self, monkeypatch):
        """回归旧 bug：正文含 'search_service' 字样不得污染 actual_source。"""
        from src.rag import retriever as retriever_mod
        from src.rag.news import NewsRetrievalResult

        fake = NewsRetrievalResult(
            block="### 近期动态\n- 2026-02-13 某公司发布 search_service 新产品",
            hit_source=SOURCE_AKSHARE_EM,
            elapsed_ms=5.0,
        )
        monkeypatch.setattr("src.rag.news.retrieve_news_ex", lambda *a, **k: fake)

        ctx = retriever_mod.retrieve_financial_context("600036", "招商银行")
        news_trace = [t for t in ctx.source_trace if t.dimension == "news"][0]

        assert news_trace.actual_source == SOURCE_AKSHARE_EM

    def test_news_exception_degrades_to_none_source(self, monkeypatch):
        from src.rag import retriever as retriever_mod

        def _boom(*args, **kwargs):
            raise RuntimeError("news down")

        monkeypatch.setattr("src.rag.news.retrieve_news_ex", _boom)

        ctx = retriever_mod.retrieve_financial_context("600036", "招商银行")
        news_trace = [t for t in ctx.source_trace if t.dimension == "news"][0]

        assert news_trace.actual_source == "none"
        assert news_trace.success is False


# ═════════════════════════════════════════════════════════════
# 7. 格式化与去重
# ═════════════════════════════════════════════════════════════
class TestFormattingAndDedup:
    def test_block_respects_5_items_and_600_chars(self):
        items = [
            {"title": f"标题{i}" * 10, "summary": "s", "date": "2026-02-13", "source": "x"}
            for i in range(20)
        ]
        block = news_mod._format_news_block(items)
        assert len(block) <= 600
        assert block.count("\n- ") <= 5

    def test_dedup_by_title_prefix(self):
        items = [
            {"title": "招商银行发布年度报告A", "date": "", "summary": "", "source": "n"},
            {"title": "招商银行发布年度报告B", "date": "", "summary": "", "source": "a"},
            {"title": "完全不同的另一条新闻", "date": "", "summary": "", "source": "a"},
        ]
        deduped = news_mod._deduplicate_news(items, prefix_chars=10)
        assert len(deduped) == 2

    def test_empty_items_yield_empty_block(self):
        assert news_mod._format_news_block([]) == ""
