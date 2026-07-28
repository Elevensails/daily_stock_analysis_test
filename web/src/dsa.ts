/* ==========================================================================
 * 内容契约访问层：从构建期注入的 window.__DSA__ 读取 manifest 与 fragments。
 * 契约 schema 见 U2-architecture.md §2。前端只消费数据，不关心后端语言。
 * ========================================================================== */

export interface SlotEntry {
  label?: string;
  name?: string;
  fragments?: Record<string, string | null>;
}

export interface DsaManifest {
  schemaVersion?: string;
  generatedAt?: string;
  model?: string;
  site?: { title?: string; base?: string };
  stats?: {
    slotsPerDay?: number;
    holdings?: number;
    reportTypes?: number;
    monthlyCost?: string;
  };
  slots?: Record<string, SlotEntry>;
  fragmentTypes?: string[];
}

export interface DsaData {
  manifest: DsaManifest | null;
  fragments: Record<string, string>;
}

declare global {
  interface Window {
    __DSA__?: DsaData;
  }
}

/** 时段渲染顺序（晚盘优先，贴近「最新收盘复盘」）。 */
export const SLOT_ORDER = ['1800', '1430', '1200', '0930', '0900'];

/** 读取注入的数据；缺失时回退为空契约（绝不崩溃）。 */
export function getDsa(): DsaData {
  return window.__DSA__ ?? { manifest: null, fragments: {} };
}

/**
 * 按契约 key 取片段 HTML。key 可为 "type_HHMM_YYYYMMDD" 或带 .html 后缀。
 * 返回已 html.escape 的正文片段，或 null（缺失）。
 */
export function getFragment(key: string | null | undefined): string | null {
  if (!key) return null;
  const k = key.replace(/\.html$/, '');
  return getDsa().fragments[k] ?? null;
}

/** 在 manifest 中按给定类型寻找最新的片段 key（晚盘优先）。 */
export function latestFragmentKey(manifest: DsaManifest | null, type: string): string | null {
  if (!manifest?.slots) return null;
  for (const code of SLOT_ORDER) {
    const f = manifest.slots[code]?.fragments?.[type];
    if (f) return f.replace(/\.html$/, '');
  }
  return null;
}

/** 从片段文件名解析日期 YYYYMMDD（如 report_1800_20250728.html → 20250728）。 */
export function dateFromFragment(file: string | null | undefined): string | null {
  if (!file) return null;
  const m = /_(\d{8})\.html$/.exec(file);
  return m ? m[1] : null;
}
