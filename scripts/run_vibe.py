#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run Vibe-Trading quant analysis for the 4 tracked A-share holdings and save
a structured markdown report + the raw JSON.

Design notes (vibe-trading non-interactive integration):
  * `vibe-trading run -p PROMPT --json` is NON-INTERACTIVE SAFE:
      - cli/main.py `_is_interactive_invocation` returns False when stdin/stdout
        are not TTYs, so the run path never enters the prompt_toolkit REPL.
      - cli/_legacy.py `main()` force-sets `no_rich=True` when stdout is not a
        TTY, so no Rich Live dashboard / TUI is started.
      - The `run` dispatch (`_handle_prompt_command` -> `cmd_run` ->
        `_run_agent`) contains NO `input()` / `getpass()` / confirmation in the
        non-interactive path; it just builds an AgentLoop and prints a clean
        `json.dumps({"status","run_id","run_dir","reason"})` via
        `_print_json_result`.
      - A real run still REQUIRES a DeepSeek/OpenRouter API key + network
        (ChatLLM via langchain). Missing key/network -> the agent raises ->
        process exits non-zero. This wrapper catches that and emits a graceful
        "暂无数据" skeleton instead of crashing.
  * Because the CLI is non-interactive safe we drive it with `subprocess`
    (stdin=DEVNULL so a hung child cannot block on a prompt; start_new_session
    so it can be killed on timeout). No pty/pexpect wrapper is needed.
  * All model-produced text is brace-escaped before being concatenated into the
    report markdown (f-string safety: vibe output may contain literal `{`/`}`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import html
from datetime import datetime, timezone, timedelta

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
VIBE_TIMEOUT_SECONDS = 1200  # hard ceiling for a single vibe run; raised from 600
# because a 4-symbol deep analysis at --max-iter 200 frequently needs >600s
# (observed 330s on a fast trajectory, >600s on a heavier one).
# Agent iterations needed to finish a 4-symbol deep analysis (data fetch +
# indicator computation per symbol). The upstream default (~50) is too low and
# makes the run end with "reached max iterations without final answer", so
# run_vibe.py would only ever emit the empty skeleton. Bumped after a real
# validation run proved 200 iterations completes a full structured report.
VIBE_MAX_ITER = 200
HOLDINGS = [
    ("600036", "招商银行"),
    ("159915", "创业板ETF"),
    ("603823", "百合花"),
    ("512400", "有色金属ETF"),
]
ANALYSIS_PROMPT = (
    "Analyze these 4 A-share holdings for short-term trading signals: "
    "600036 招商银行, 159915 创业板ETF, 603823 百合花, 512400 有色金属ETF. "
    "For each symbol, provide under its own heading: technical indicators "
    "(MA/RSI/MACD), support/resistance levels, volume analysis, and a "
    "buy/sell/hold recommendation. Output as structured markdown, one section "
    "per symbol, with sub-headings 技术面 / 支撑阻力 / 量能 / 买卖建议."
)

now = datetime.now(timezone(timedelta(hours=8)))
tslot = os.environ.get("TIME_SLOT", now.strftime("%H%M"))
today = now.strftime("%Y%m%d")
workspace = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _escape_braces(text: str) -> str:
    """Make arbitrary model text safe to embed in our (brace-free) markdown.

    Per project f-string safety rule: literal `{`/`}` from untrusted output are
    converted to HTML entity references so they can never be interpreted as a
    format field if a future editor switches the assembly to an f-string.
    """
    return text.replace("{", "&#123;").replace("}", "&#125;")


def _safe_write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _ensure_pip(pkg: str) -> bool:
    """Best-effort install; never fatal (sandbox/network may block it)."""
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
            check=False,
            timeout=300,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - best effort only
        print(f"[warn] pip install {pkg} skipped/failed: {exc}")
        return False


def _run_vibe_cli(prompt: str) -> "tuple[int, str, str]":
    """Execute `vibe-trading run -p PROMPT --json --no-rich`.

    Returns (returncode, stdout, stderr). Uses subprocess (non-interactive
    safe). stdin is DEVNULL and the process runs in its own session so a hang
    can be killed by the timeout.
    """
    base_args = [
        "run",
        "-p",
        prompt,
        "--json",
        "--no-rich",
        "--max-iter",
        str(VIBE_MAX_ITER),
    ]
    # Prefer the console script; fall back to `python -m cli`.
    candidates = [
        ["vibe-trading", *base_args],
        [sys.executable, "-m", "cli", *base_args],
    ]
    last_err = ""
    for cmd in candidates:
        try:
            proc = subprocess.run(
                cmd,
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=VIBE_TIMEOUT_SECONDS,
                start_new_session=True,
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except FileNotFoundError as exc:
            last_err = str(exc)
            continue
        except subprocess.TimeoutExpired as exc:
            return (
                124,
                (exc.stdout or "") if isinstance(exc.stdout, str) else "",
                f"vibe-trading run timed out after {VIBE_TIMEOUT_SECONDS}s",
            )
        except Exception as exc:  # noqa: BLE001 - surface as failure
            last_err = str(exc)
            continue
    return 127, "", f"vibe-trading executable not found: {last_err}"


def _load_report_artifact(run_dir: "str | None") -> "str | None":
    """Best-effort load of the actual analysis markdown from vibe's run dir.

    `run --json` only returns a status/run_id/run_dir summary; the real prose
    lives in the run artifact (report.md / result.md / trace.jsonl). We try the
    common filenames and fall back to scanning trace.jsonl for an 'answer'.
    """
    if not run_dir:
        return None
    run_path = os.path.join(workspace, run_dir) if not os.path.isabs(run_dir) else run_dir
    if not os.path.isdir(run_path):
        # run_dir may be relative to vibe's own runtime root; try as-is too.
        if not os.path.isdir(run_dir):
            return None
        run_path = run_dir
    for name in ("report.md", "result.md", "analysis.md", "output.md"):
        candidate = os.path.join(run_path, name)
        if os.path.isfile(candidate):
            try:
                return _escape_braces(_read_text(candidate))
            except OSError:
                continue
    trace = os.path.join(run_path, "trace.jsonl")
    if os.path.isfile(trace):
        try:
            return _escape_braces(_extract_answer_from_trace(trace))
        except OSError:
            return None
    return None


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _extract_answer_from_trace(trace_path: str) -> str:
    answer_parts: list[str] = []
    with open(trace_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "answer" and obj.get("content"):
                answer_parts.append(obj["content"])
    return "\n\n".join(answer_parts)


def _split_sections_by_symbol(raw_md: str) -> "dict[str, str]":
    """Split a merged report into per-symbol text blocks using code/name anchors."""
    blocks: "dict[str, str]" = {}
    # Build a regex that matches a heading line containing any holding code/name.
    anchors = [re.escape(code) for code, _ in HOLDINGS] + [
        re.escape(name) for _, name in HOLDINGS
    ]
    # Heading line that CONTAINS any holding code/name anywhere after the '#'
    # markers. Allowing `.*?` between '#' and the anchor tolerates prefixes the
    # agent emits (e.g. "## 一、600036.SH 招商银行"); dropping the `[*#\-]*`
    # alternative avoids false matches on bulleted list items.
    pattern = re.compile(
        r"^[ \t]*#{1,6}[ \t]*.*?(" + "|".join(anchors) + r")",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(raw_md))
    if not matches:
        # No per-symbol anchors: treat the whole doc as one combined block.
        return {"__all__": raw_md.strip()}
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw_md)
        block = raw_md[start:end].strip()
        # Determine which symbol this block belongs to (first matching anchor).
        header = raw_md[match.start():match.end()]
        owner = None
        for code, name in HOLDINGS:
            if code in header or name in header:
                owner = code
                break
        if owner is None:
            owner = "__all__"
        blocks.setdefault(owner, "")
        if blocks[owner]:
            blocks[owner] += "\n\n" + block
        else:
            blocks[owner] = block
    return blocks


def _extract_subsection(block: str, keywords: "tuple[str, ...]") -> str:
    """Pull the lines under the FIRST heading matching any keyword, then stop.

    Only the first matching subsection is taken (we break as soon as its body
    ends on the next heading) so that a per-symbol block never leaks another
    symbol's same-named subsection into this one.
    """
    lines = block.splitlines()
    captured: list[str] = []
    active = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            if any(kw in stripped for kw in keywords):
                active = True
                continue
            # A heading ends the (first) captured subsection -> stop entirely.
            if active:
                break
            active = False
            continue
        if active:
            captured.append(line)
    return "\n".join(captured).strip()


_SUBSECTIONS = [
    ("技术面", ("技术面", "技术指标", "MA", "RSI", "MACD", "technical")),
    ("支撑阻力", ("支撑阻力", "支撑", "阻力", "support", "resistance")),
    ("量能", ("量能", "成交量", "volume", "放量", "缩量")),
    ("买卖建议", ("买卖建议", "建议", "买入", "卖出", "持有", "评级", "recommend")),
]


def _structure_report(raw_md: str) -> str:
    """Render a per-symbol structured markdown with the 4 required subsections."""
    if not raw_md or not raw_md.strip():
        return ""
    blocks = _split_sections_by_symbol(raw_md)
    out_parts: list[str] = []
    for code, name in HOLDINGS:
        block = blocks.get(code, blocks.get("__all__", ""))
        out_parts.append(f"## {code} {name}")
        if not block.strip():
            out_parts.append("\n暂无数据：本次 vibe-trading 运行未返回该标的的分析。\n")
            continue
        for title, keywords in _SUBSECTIONS:
            sub = _extract_subsection(block, keywords)
            out_parts.append(f"### {title}")
            out_parts.append(sub if sub else "（未在报告中提供）")
            out_parts.append("")
    return "\n".join(out_parts).strip() + "\n"


def _clean_reason(raw_reason: str, rc: int, has_key: bool) -> str:
    """清洗面向用户的错误文案，绝不暴露 traceback / 内部堆栈细节。

    上游 vibe-trading-ai 崩溃时 stderr 首行通常是 ``Traceback (most recent
    call last):``，直接透传给量化页面既不友好又暴露实现细节，故统一替换为友好
    提示。判定优先级（最具体的已知失败原因优先）：
      1. rc == 124（超时）：运行超时提示
      2. 缺少 DEEPSEEK_API_KEY：沙箱环境常见，run 根本未执行
      3. 空 / 仅空白：未知错误
      4. 含 ``traceback``（不区分大小写）：上游分析服务异常提示
      5. 其他：保留原样并截断到 120 字符，避免单行过长
    """
    if rc == 124:
        return f"运行超时（>{VIBE_TIMEOUT_SECONDS}s）"
    if not has_key:
        return "缺少 DEEPSEEK_API_KEY（沙箱环境常见），vibe-trading run 未执行"
    reason = (raw_reason or "").strip()
    if not reason:
        return "未知错误"
    if "traceback" in reason.lower():
        return "上游分析服务异常（请检查 API Key / 网络连接 / vibe-trading-ai 依赖版本）"
    return reason[:120]


def _build_error_report(reason: str) -> str:
    """Graceful skeleton when vibe could not run (missing key/network/timeout)."""
    lines = [
        "# Vibe-Trading 量化分析",
        "",
        f"> 运行时间: {now.strftime('%Y-%m-%d %H:%M')} | 模型: DeepSeek",
        "",
        f"> ⚠️ 本次 vibe-trading 未能完成分析：{html.escape(reason)}",
        "",
        "> 以下为按标的预留的结构化骨架，待服务恢复后由下次运行填充：",
        "",
    ]
    for code, name in HOLDINGS:
        lines.append(f"## {code} {name}")
        for title, _ in _SUBSECTIONS:
            lines.append(f"### {title}")
            lines.append("暂无数据：vibe-trading run 未执行或无可用结果。")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    print("Ensuring vibe-trading-ai is available...")
    _ensure_pip("vibe-trading-ai")

    # Write DeepSeek credentials from the environment into agent/.env.
    os.makedirs(os.path.join(workspace, "agent"), exist_ok=True)
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    env_content = (
        "LANGCHAIN_PROVIDER=deepseek\n"
        f"DEEPSEEK_API_KEY={deepseek_key}\n"
        "DEEPSEEK_BASE_URL=https://api.deepseek.com\n"
        "LANGCHAIN_MODEL_NAME=deepseek-chat\n"
    )
    _safe_write(os.path.join(workspace, "agent", ".env"), env_content)

    print(f"Running vibe-trading with tslot={tslot}...")
    rc, stdout, stderr = _run_vibe_cli(ANALYSIS_PROMPT)

    os.makedirs(os.path.join(workspace, "reports"), exist_ok=True)
    json_path = os.path.join(workspace, "reports", f"vibe_{tslot}_{today}.json")
    md_path = os.path.join(workspace, "reports", f"vibe_{tslot}_{today}.md")

    # Always persist the raw CLI stdout for debugging / downstream consumers.
    _safe_write(json_path, stdout or "")

    raw_report = None
    if rc == 0 and stdout.strip():
        try:
            data = json.loads(stdout)
            run_dir = data.get("run_dir")
            if data.get("status") in ("success", None) and run_dir:
                raw_report = _load_report_artifact(run_dir)
            elif data.get("status") == "failed":
                reason = data.get("reason") or "分析执行失败"
                raw_report = None
                stderr = stderr or reason
        except json.JSONDecodeError:
            # stdout was not the status JSON; treat it as the report body.
            raw_report = _escape_braces(stdout)

    if raw_report:
        structured = _structure_report(raw_report)
        if not structured:
            structured = raw_report
        header = (
            "# Vibe-Trading 量化分析\n\n"
            f"> 运行时间: {now.strftime('%Y-%m-%d %H:%M')} | 模型: DeepSeek\n\n"
        )
        _safe_write(md_path, header + structured)
        print(f"Saved: {json_path}")
        print(f"Saved: {md_path} ({len(structured)} chars structured)")
        return 0

    # Graceful failure path: emit a skeleton so deploy_pages.py still has input.
    # 取 stderr 首行作为原始原因，再经 _clean_reason 清洗为面向用户的友好文案
    # （上游崩溃时首行往往是 Traceback，必须替换，不可透传给量化页面）。
    first_line = (
        (stderr or "").strip().splitlines()[0] if (stderr or "").strip() else ""
    )
    reason = _clean_reason(first_line, rc, bool(deepseek_key))
    _safe_write(md_path, _build_error_report(reason))
    print(f"[warn] vibe-trading run 未完成 (rc={rc}); wrote skeleton report.")
    print(f"  reason: {reason}")
    print(f"Saved: {json_path}")
    print(f"Saved: {md_path}")
    # Non-zero so the scheduler knows the analysis itself did not succeed,
    # but the report artifact exists for downstream steps.
    return rc if rc not in (0,) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - never crash the scheduled run
        print(f"[error] run_vibe.py failed: {exc}")
        sys.exit(2)
