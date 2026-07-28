/* ==========================================================================
 * 轻量 DOM 工具：el 创建节点、clear 清空、highlight 涨红跌绿着色。
 * 无框架依赖；所有注入内容已先经 md2html(html.escape)，此处仅包裹安全 span。
 * ========================================================================== */

export interface ElProps {
  class?: string;
  id?: string;
  href?: string;
  title?: string;
  text?: string;
  html?: string;
  dataset?: Record<string, string>;
  attrs?: Record<string, string>;
  onClick?: (e: MouseEvent) => void;
}

/** 创建元素（支持 class/id/href/text/html/dataset/attrs/onClick）。 */
export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: ElProps = {},
  children: (Node | string)[] = []
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (props.class) node.className = props.class;
  if (props.id) node.id = props.id;
  if (props.href) node.setAttribute('href', props.href);
  if (props.title) node.setAttribute('title', props.title);
  if (props.text !== undefined) node.textContent = props.text;
  if (props.html !== undefined) node.innerHTML = props.html;
  if (props.dataset) {
    for (const [k, v] of Object.entries(props.dataset)) {
      node.dataset[k] = v;
    }
  }
  if (props.attrs) {
    for (const [k, v] of Object.entries(props.attrs)) {
      node.setAttribute(k, v);
    }
  }
  if (props.onClick) node.addEventListener('click', props.onClick as EventListener);
  for (const c of children) {
    node.append(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

/** 清空元素所有子节点。 */
export function clear(node: HTMLElement): void {
  node.replaceChildren();
}

const SIGNED_RE = /([+\-]\d+(?:\.\d+)?%?)/g;

/**
 * 对容器内文本中的「带符号数值/百分比」着色：+x 涨红、-x 跌绿（A 股习惯）。
 * 仅包裹 <span>，不改动既有结构；内容已 html.escape，安全。
 */
export function colorize(root: HTMLElement): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const targets: Text[] = [];
  let n: Node | null;
  while ((n = walker.nextNode())) {
    const t = n as Text;
    if (t.nodeValue && SIGNED_RE.test(t.nodeValue)) targets.push(t);
  }
  for (const t of targets) {
    const value = t.nodeValue ?? '';
    const frag = document.createDocumentFragment();
    let last = 0;
    let m: RegExpExecArray | null;
    let changed = false;
    SIGNED_RE.lastIndex = 0;
    while ((m = SIGNED_RE.exec(value))) {
      changed = true;
      if (m.index > last) frag.append(document.createTextNode(value.slice(last, m.index)));
      const token = m[1];
      const span = document.createElement('span');
      span.className = token.startsWith('-') ? 'down' : 'up';
      span.textContent = token;
      frag.append(span);
      last = m.index + token.length;
    }
    if (changed) {
      if (last < value.length) frag.append(document.createTextNode(value.slice(last)));
      t.parentNode?.replaceChild(frag, t);
    }
  }
}
