# -*- coding: utf-8 -*-
"""Mock 测试：验证 P1.6 CI 探测脚本的解析与 JSON 输出（无网络）。

覆盖点：
- to_json 可往返解析；
- run_probes 在 mock akshare / mock requests 下能正确产出 ok/empty/error/skip；
- main() 在依赖缺失 / 全部抛异常时仍返回 0 且输出合法 JSON；
- --out 能原子写出可解析的 JSON 文件。
"""

import importlib.util
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# 动态导入脚本（scripts 无 __init__.py，按路径加载，避免污染包结构）
_spec = importlib.util.spec_from_file_location(
    "probe_datasource_ci", os.path.join(SCRIPTS_DIR, "probe_datasource_ci.py")
)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


# ── 假对象 ──
class FakeRow(dict):
    def to_dict(self) -> dict:
        return dict(self)


class FakeDataFrame:
    """模拟 pandas.DataFrame 的最小 duck type（支持 iloc / len / empty）。"""

    def __init__(self, rows):
        self._rows = list(rows)

    @property
    def empty(self) -> bool:
        # 与真实 pandas 一致：0 行时 empty 为 True
        return len(self._rows) == 0

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def iloc(self):
        rows = self._rows

        class _Iloc:
            def __getitem__(_self, i):
                return FakeRow(rows[i])

        return _Iloc()


class FakeAkshare:
    def __init__(self, cyq_rows, news_rows):
        self._cyq = FakeDataFrame(cyq_rows)
        self._news = FakeDataFrame(news_rows)

    def stock_cyq_em(self, symbol=None, **_kw):
        # 仅 600036 有数据，603823 返回空，以贴合 run_probes 的双标的语义
        if symbol == "600036":
            return self._cyq
        return FakeDataFrame([])

    def stock_news_em(self, symbol=None, **_kw):
        if symbol == "600036":
            return self._news
        return FakeDataFrame([])


class FakeAkshareRaising:
    def stock_cyq_em(self, symbol=None, **_kw):
        raise RuntimeError("boom-cyq")

    def stock_news_em(self, symbol=None, **_kw):
        raise RuntimeError("boom-news")


class FakeResponse:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class FakeRequests:
    def __init__(self, status_code=200, text="ok"):
        self._resp = FakeResponse(status_code, text)

    def get(self, url, **_kw):
        return self._resp


class FakeRequestsRaising:
    def get(self, url, **_kw):
        raise RuntimeError("boom-raw")


# ── 测试 ──
def test_to_json_roundtrip():
    report = probe.run_probes(akshare=FakeAkshare([{"a": 1}], [{"b": 2}]), requests_mod=FakeRequests())
    text = probe.to_json(report)
    parsed = json.loads(text)  # 不抛异常即通过
    assert parsed["tool"] == "probe_datasource_ci"
    # 600036 ok / 603823 empty → 整体应为 partial
    assert parsed["summary"]["overall"] == "partial"


def test_probe_chip_ok():
    fake = FakeAkshare([{"日期": "2026-01-01", "获利比例": 0.5}], [])
    res = probe.probe_chip_one("600036", akshare=fake)
    assert res["status"] == "ok"
    assert res["rows"] == 1
    assert isinstance(res["snippet"], dict)
    assert res["latency_ms"] >= 0


def test_probe_chip_empty():
    fake = FakeAkshare([], [])
    res = probe.probe_chip_one("600036", akshare=fake)
    assert res["status"] == "empty"


def test_probe_chip_error_caught():
    res = probe.probe_chip_one("600036", akshare=FakeAkshareRaising())
    assert res["status"] == "error"
    assert "boom-cyq" in res["error"]


def test_probe_news_ok_and_empty():
    fake = FakeAkshare([{"x": 1}], [{"新闻标题": "t1"}, {"新闻标题": "t2"}])
    ok = probe.probe_news_one("600036", akshare=fake)
    assert ok["status"] == "ok"
    assert ok["items"] == 2

    empty_fake = FakeAkshare([], [])
    empty = probe.probe_news_one("600036", akshare=empty_fake)
    assert empty["status"] == "empty"


def test_probe_raw_ok_and_error():
    ok = probe.probe_raw_one("quote", probe.EASTMONEY_QUOTE_URL, requests_mod=FakeRequests(200, "data"))
    assert ok["status"] == "ok"
    assert ok["http_status"] == 200

    bad = probe.probe_raw_one("quote", probe.EASTMONEY_QUOTE_URL, requests_mod=FakeRequests(503, "no"))
    assert bad["status"] == "error"
    assert bad["http_status"] == 503

    raised = probe.probe_raw_one("quote", probe.EASTMONEY_QUOTE_URL, requests_mod=FakeRequestsRaising())
    assert raised["status"] == "error"


def test_run_probes_summary_counts():
    report = probe.run_probes(
        akshare=FakeAkshare([{"a": 1}], [{"b": 2}]),
        requests_mod=FakeRequests(200, "ok"),
    )
    s = report["summary"]
    assert s["chip_ok"] == 1  # 600036 ok, 603823 empty
    assert s["chip_empty"] == 1
    assert s["news_ok"] == 1
    assert s["raw_ok"] == 2
    assert s["overall"] == "partial"


def test_run_probes_skip_when_lib_missing(monkeypatch):
    def _raise(*_a, **_k):
        raise ImportError("missing")

    monkeypatch.setattr(probe, "_import_akshare", _raise)
    monkeypatch.setattr(probe, "_import_requests", _raise)
    report = probe.run_probes()
    for entry in report["targets"]["chip"].values():
        assert entry["status"] == "skip"
    for entry in report["targets"]["news"].values():
        assert entry["status"] == "skip"
    for entry in report["targets"]["raw_eastmoney"].values():
        assert entry["status"] == "skip"
    assert report["summary"]["overall"] == "all_failed"


def test_main_returns_zero_on_full_failure(monkeypatch, capsys):
    monkeypatch.setattr(probe, "_import_akshare", lambda: (_ for _ in ()).throw(ImportError("no-akshare")))
    monkeypatch.setattr(probe, "_import_requests", lambda: (_ for _ in ()).throw(ImportError("no-requests")))
    rc = probe.main([])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["tool"] == "probe_datasource_ci"


def test_main_writes_atomic_out_file(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "_import_akshare", lambda: FakeAkshare([{"a": 1}], [{"b": 2}]))
    monkeypatch.setattr(probe, "_import_requests", lambda: FakeRequests(200, "ok"))
    out_path = tmp_path / "probe-result.json"
    rc = probe.main(["--out", str(out_path)])
    assert rc == 0
    assert out_path.exists()
    parsed = json.loads(out_path.read_text(encoding="utf-8"))
    assert parsed["summary"]["raw_ok"] == 2
