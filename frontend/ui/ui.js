/* Unprompted interaction kit, shared by every page. No dependencies, no build step. Load it in <head> (it applies the
   saved theme, motion and present-mode preferences before first paint) and use window.U. API and examples:
   frontend/ui/README.md and /ui/kit.html.

   Rules (reports/Interactive UI overhaul for Unprompted.md): motion only as feedback and under 300 ms; nothing
   animates on keyboard or repeated actions; every hover has a focus equivalent; single-key shortcuts can be turned off;
   newer platform features (View Transitions, anchor positioning) are enhancements with plain fallbacks. */
(() => {
  const root = document.documentElement;
  const mqReduce = matchMedia('(prefers-reduced-motion: reduce)');

  // ---------------------------------------------------------------- preferences (applied before first paint)
  const PREFS_KEY = 'u-prefs';
  const defaults = { theme: 'light', motion: 'system', keys: true, present: false, density: 'comfortable' };
  let prefs = { ...defaults };
  try { prefs = { ...defaults, ...JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') }; } catch {}
  if (new URLSearchParams(location.search).get('present') === '1') prefs.present = true;
  function applyPrefs() {
    root.dataset.theme = prefs.theme;
    if (prefs.motion === 'reduce') root.dataset.motion = 'reduce'; else delete root.dataset.motion;
    if (prefs.present) root.dataset.present = ''; else delete root.dataset.present;
    root.dataset.density = prefs.density;
  }
  applyPrefs();
  const reduced = () => prefs.motion === 'reduce' || mqReduce.matches;
  function setPref(key, value) {
    prefs[key] = value;
    try { localStorage.setItem(PREFS_KEY, JSON.stringify({ ...prefs, present: false })); } catch {}   // present mode is per visit
    applyPrefs();
    document.dispatchEvent(new CustomEvent('u:prefs', { detail: { ...prefs } }));
  }

  // ---------------------------------------------------------------- DOM helpers
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  // Strings become text nodes, so API data never reaches innerHTML.
  function h(tag, attrs, ...kids) {
    const el = tag.includes(':') ? document.createElementNS('http://www.w3.org/2000/svg', tag.split(':')[1]) : document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'style' && v && typeof v === 'object') for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
      else if (v != null && v !== false) el.setAttribute(k, v === true ? '' : v);
    }
    el.append(...kids.flat(Infinity).filter((k) => k != null && k !== false && k !== ''));
    return el;
  }
  const svg = (tag, attrs, ...kids) => h('svg:' + tag, attrs, ...kids);

  // ---------------------------------------------------------------- formatting
  const nfCompact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
  const nfPlain = new Intl.NumberFormat();
  function fmt(n, kind) {
    if (n == null || Number.isNaN(Number(n))) return '–';
    if (kind === 'compact') return nfCompact.format(n);
    if (kind === 'money') return '$' + (Math.abs(n) >= 10000 ? nfCompact.format(n) : nfPlain.format(Math.round(n)));
    if (kind === 'pct') return `${Math.round(n)}%`;
    return nfPlain.format(Math.round(n));
  }
  const clock = (s) => (s == null ? '' : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  function ago(iso) {
    if (!iso) return '';
    const s = (new Date(iso).getTime() - Date.now()) / 1000, a = Math.abs(s);
    if (a < 60) return rtf.format(Math.round(s), 'second');
    if (a < 3600) return rtf.format(Math.round(s / 60), 'minute');
    if (a < 86400) return rtf.format(Math.round(s / 3600), 'hour');
    if (a < 86400 * 45) return rtf.format(Math.round(s / 86400), 'day');
    if (a < 86400 * 365) return rtf.format(Math.round(s / (86400 * 30)), 'month');
    return rtf.format(Math.round(s / (86400 * 365)), 'year');
  }
  const date = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '');

  // ---------------------------------------------------------------- view transitions
  // U.transition(update): animate a DOM change where supported. Never for keyboard moves.
  function transition(update) {
    if (reduced() || !document.startViewTransition) { update(); return Promise.resolve(); }
    return document.startViewTransition(update).finished.catch(() => {});
  }

  // ---------------------------------------------------------------- toasts
  let toastBox;
  function toast(message, opts = {}) {
    if (!toastBox) {
      toastBox = h('ol', { class: 'u-toasts', 'aria-label': 'Notifications' });
      document.body.append(toastBox);
    }
    const kind = opts.kind || 'info', sticky = kind === 'error' || opts.action || opts.sticky;
    const close = () => { li.classList.add('out'); setTimeout(() => li.remove(), 220); };
    const li = h('li', { class: `u-toast u-toast-${kind}`, role: kind === 'error' ? 'alert' : 'status' },
      h('span', {}, message),
      opts.action ? h('button', { type: 'button', onclick: () => { close(); opts.action.run(); } }, opts.action.label) : null,
      h('button', { type: 'button', class: 'u-x', 'aria-label': 'Dismiss', onclick: close }, '×'));
    toastBox.append(li);
    while (toastBox.children.length > 4) toastBox.firstElementChild.remove();
    if (!sticky) setTimeout(close, opts.duration || Math.min(8000, 2600 + String(message).length * 40));
    return close;
  }

  // ---------------------------------------------------------------- optimistic changes (never for money or rights)
  // U.optimistic({apply, send, revert, done: 'Paused', undo: () => ...}) applies now, reverts with a retry toast on failure.
  async function optimistic({ apply, send, revert, done, undo, retry = true }) {
    apply();
    try {
      const out = await send();
      if (done) toast(done, undo ? { action: { label: 'Undo', run: undo } } : { kind: 'ok' });
      return out;
    } catch (e) {
      revert();
      toast(e.message || 'That didn’t save.', retry ? { kind: 'error', action: { label: 'Retry', run: () => optimistic({ apply, send, revert, done, undo, retry }) } }
        : { kind: 'error' });
      throw e;
    }
  }

  // ---------------------------------------------------------------- copy to clipboard with feedback
  async function copy(text, what = 'Copied') {
    try { await navigator.clipboard.writeText(text); toast(what, { kind: 'ok' }); return true; }
    catch { window.prompt('Copy this:', text); return false; }
  }

  // ---------------------------------------------------------------- count-up numbers (first view only)
  // <b data-u-count="48200" data-u-format="compact"></b>. The final value is what assistive tech reads.
  function countUp(el) {
    const to = Number(el.dataset.uCount), kind = el.dataset.uFormat;
    if (!Number.isFinite(to)) return;
    const final = fmt(to, kind);
    el.setAttribute('aria-label', final);
    if (reduced() || to === 0) { el.textContent = final; return; }
    const start = performance.now(), dur = 700;
    const step = (now) => {
      const t = Math.min(1, (now - start) / dur), e = 1 - Math.pow(1 - t, 4);
      el.textContent = t < 1 ? fmt(to * e, kind) : final;
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      io.unobserve(e.target);
      if (e.target.dataset.uCount != null) countUp(e.target);
      if (e.target.classList.contains('u-reveal')) e.target.classList.add('in');
    }
  }, { rootMargin: '0px 0px -6% 0px' });

  // ---------------------------------------------------------------- the receipt
  // U.receipt({quote: {text, hit: [a, b], start}, frame, url, platform, handle, views, publishedAt, kind, label},
  //           {size: 'sm'|'md'|'lg', bare, actions: [elements], onTime: (seconds) => {}, spot, draw})
  const PLATFORM = { tiktok: 'TikTok', instagram: 'Instagram' };
  const KIND = { spoken: 'Said on camera', tagged: 'Tagged', sponsored: 'Disclosed ad', owned: 'Brand account', unverified: 'Fuzzy match' };
  function quoteEl(q, { draw } = {}) {
    const text = (q && q.text) || '', hit = q && q.hit;
    const kids = [];
    if (hit && text) {
      const a = Math.max(0, hit[0] - 160), z = Math.min(text.length, hit[1] + 160);
      kids.push((a ? '…' : '') + text.slice(a, hit[0]), h('mark', { class: draw && !reduced() ? 'u-mark-draw' : null }, text.slice(hit[0], hit[1])),
        text.slice(hit[1], z) + (z < text.length ? '…' : ''));
    } else kids.push(text);
    return h('blockquote', { class: 'u-quote', dir: 'auto' }, ...kids);
  }
  function receipt(r, o = {}) {
    const q = r.quote || {}, start = q.start;
    const frameSrc = r.frame || q.frame;
    const frame = h('a', { class: 'u-receipt-frame', href: r.url || '#', target: '_blank', rel: 'noopener', 'aria-label': `Open the video${r.handle ? ` by @${r.handle}` : ''}` },
      frameSrc ? h('img', { src: frameSrc, alt: '', loading: 'lazy', width: 72, height: 128, onerror: (e) => e.target.remove() }) : null,
      h('span', { class: 'u-play', 'aria-hidden': 'true' }));
    const time = start != null
      ? h(o.onTime ? 'button' : 'a', o.onTime ? { type: 'button', class: 'u-time', onclick: () => o.onTime(start), 'aria-label': `Play from ${clock(start)}` }
        : { class: 'u-time', href: r.url || '#', target: '_blank', rel: 'noopener', 'aria-label': `Said at ${clock(start)}` }, clock(start))
      : null;
    const who = h('div', { class: 'u-receipt-who' },
      r.handle ? h('b', {}, h('bdi', {}, '@' + r.handle)) : null,
      r.platform ? h('span', {}, PLATFORM[r.platform] || r.platform) : null,
      r.kind ? h('span', { class: `u-kind u-kind-${r.kind}` }, r.label || KIND[r.kind] || r.kind) : null);
    const meta = h('div', { class: 'u-receipt-meta' }, time,
      o.views === false ? null : r.views ? h('span', {}, `${fmt(r.views, 'compact')} views`) : h('span', { class: 'u-muted' }, 'views unavailable'),
      r.publishedAt ? h('span', { title: date(r.publishedAt) }, date(r.publishedAt)) : null,
      o.extraMeta || null);
    const el = h('article', { class: ['u-receipt', o.size && o.size !== 'md' ? `u-receipt-${o.size}` : '', o.bare ? 'u-receipt-bare' : '', o.spot ? 'u-spot' : ''].filter(Boolean).join(' ') },
      frame, h('div', { class: 'u-receipt-body' }, who, q.text || r.caption ? quoteEl(q.text ? q : { text: r.caption }, o) : null, meta,
        o.actions && o.actions.length ? h('div', { class: 'u-receipt-actions' }, ...o.actions) : null));
    return el;
  }

  // ---------------------------------------------------------------- score ring, band, component meter, "why"
  const band = (v) => (v >= 75 ? 'Strong' : v >= 50 ? 'Good' : v >= 25 ? 'Some' : 'Weak');
  function scoreRing(value, { size = 44, label = 'Advocate score', why } = {}) {
    const v = Math.max(0, Math.min(100, Math.round(value || 0)));
    const ring = h(why ? 'button' : 'span', { class: 'u-ring', type: why ? 'button' : null, style: { '--size': `${size}px`, '--v': v },
                                             'aria-label': `${label} ${v} of 100, ${band(v)}${why ? ': show why' : ''}` },
      svg('svg', { viewBox: '0 0 44 44', 'aria-hidden': 'true' }, svg('circle', { class: 'track', cx: 22, cy: 22, r: 18, pathLength: 100 }),
        svg('circle', { class: 'fill', cx: 22, cy: 22, r: 18, pathLength: 100 })),
      h('b', { 'aria-hidden': 'true' }, String(v)));
    if (why) ring.addEventListener('click', () => card.toggle(ring, why));
    return ring;
  }
  // parts: [{label, value, max, detail}] -> the segmented bar
  function meter(parts) {
    const total = parts.reduce((t, p) => t + p.max, 0) || 1;
    return h('span', { class: 'u-meter', role: 'img', 'aria-label': parts.map((p) => `${p.label} ${p.value} of ${p.max}`).join(', ') },
      ...parts.map((p) => h('i', { style: { width: `${(p.value / total) * 100}%` }, title: `${p.label}: ${p.value}/${p.max}` })));
  }
  // The "why this rank" body: rows of signals in words, plus an optional counterfactual line.
  function why({ title, parts, note }) {
    return h('div', { class: 'u-why' }, title ? h('h4', {}, title) : null,
      ...parts.map((p) => h('div', { class: 'u-why-row' }, h('span', {}, p.label, p.detail ? h('span', { class: 'u-muted' }, ` · ${p.detail}`) : null),
        h('span', {}, `${p.value}/${p.max}`), h('i', {}, h('b', { style: { '--v': Math.round((p.value / p.max) * 100) } })))),
      note ? h('p', {}, note) : null, h('span', { class: 'u-computed-tag' }, 'Computed'));
  }

  // ---------------------------------------------------------------- sparkline
  // U.sparkline([v1, v2, ...], {mark: index, label}) -> inline SVG; the marked point is where the brand was said.
  function sparkline(values, { mark = -1, label = '', w = 96, ht = 24 } = {}) {
    const vals = values.map((v) => Number(v) || 0), max = Math.max(1, ...vals), n = Math.max(1, vals.length - 1);
    const pts = vals.map((v, i) => [(i / n) * w, ht - 2 - (v / max) * (ht - 4)]);
    const el = svg('svg', { class: 'u-spark', viewBox: `0 0 ${w} ${ht}`, role: 'img', 'aria-label': label || `${vals.length} values` },
      svg('path', { d: pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ') }));
    if (mark >= 0 && pts[mark]) el.append(svg('circle', { cx: pts[mark][0], cy: pts[mark][1], r: 3 }));
    return el;
  }

  // ---------------------------------------------------------------- segmented controls and tabs (sliding indicators)
  function seg(rootEl) {
    if (rootEl.dataset.uSegOn) return;
    rootEl.dataset.uSegOn = '1';
    const thumb = h('span', { class: 'u-seg-thumb', 'aria-hidden': 'true' });
    rootEl.prepend(thumb);
    const move = () => {
      const on = $$(':scope > label, :scope > button', rootEl).find((b) => b.getAttribute('aria-pressed') === 'true' || $('input:checked', b));
      thumb.style.opacity = on ? 1 : 0;
      if (!on) return;
      thumb.style.width = `${on.offsetWidth}px`;
      thumb.style.transform = `translateX(${on.offsetLeft - 3}px)`;
    };
    requestAnimationFrame(move);
    new MutationObserver(move).observe(rootEl, { subtree: true, attributes: true, attributeFilter: ['aria-pressed', 'checked'] });
    rootEl.addEventListener('change', move);
    rootEl.addEventListener('click', () => requestAnimationFrame(move));
    const form = rootEl.closest('form');
    if (form) form.addEventListener('reset', () => requestAnimationFrame(move));
    new ResizeObserver(move).observe(rootEl);
    rootEl.uSync = move;                     // U.seg.sync(el) after setting .checked from code
  }
  seg.sync = (el) => (el && el.uSync ? el.uSync() : null);
  function tabs(rootEl, onChange) {
    const bar = h('span', { class: 'u-tabs-bar', 'aria-hidden': 'true' });
    rootEl.append(bar);
    const all = () => $$('[role="tab"]', rootEl);
    const select = (tab, focus) => {
      all().forEach((t) => { const on = t === tab; t.setAttribute('aria-selected', String(on)); t.tabIndex = on ? 0 : -1;
        const panel = t.getAttribute('aria-controls') && document.getElementById(t.getAttribute('aria-controls')); if (panel) panel.hidden = !on; });
      bar.style.width = `${tab.offsetWidth}px`; bar.style.transform = `translateX(${tab.offsetLeft}px)`;
      if (focus) tab.focus();
      onChange && onChange(tab);
    };
    rootEl.addEventListener('click', (e) => { const t = e.target.closest('[role="tab"]'); if (t) select(t); });
    rootEl.addEventListener('keydown', (e) => {
      const list = all(), i = list.indexOf(document.activeElement);
      if (i < 0) return;
      const dir = getComputedStyle(rootEl).direction === 'rtl' ? -1 : 1;
      if (e.key === 'ArrowRight') { e.preventDefault(); select(list[(i + dir + list.length) % list.length], true); }
      if (e.key === 'ArrowLeft') { e.preventDefault(); select(list[(i - dir + list.length) % list.length], true); }
    });
    const currentTab = () => all().find((t) => t.getAttribute('aria-selected') === 'true') || all()[0];
    const measure = () => { const t = currentTab(); if (t) { bar.style.width = `${t.offsetWidth}px`; bar.style.transform = `translateX(${t.offsetLeft}px)`; } };
    requestAnimationFrame(() => select(currentTab()));
    new ResizeObserver(measure).observe(rootEl);   // initialised while hidden: measures once it's shown
    return { select, refresh: measure };
  }

  // ---------------------------------------------------------------- hover cards and tooltips
  // [data-u-tip="text"] shows a tooltip. U.card.provide(selector, (anchor) => element) makes hover/focus on matching
  // elements open a card with that content (e.g. the full receipt behind a quote marker). Hoverable, Esc closes,
  // first open waits 350 ms, then instant while moving between anchors; 200 ms grace when leaving.
  const providers = [];
  let pop, popFor = null, openTimer = 0, closeTimer = 0, warmUntil = 0;
  function popEl() {
    if (!pop) {
      pop = h('div', { class: 'u-pop', popover: 'manual', id: 'u-pop' });
      pop.addEventListener('pointerenter', () => clearTimeout(closeTimer));
      pop.addEventListener('pointerleave', () => scheduleClose());
      document.body.append(pop);
    }
    return pop;
  }
  function placePop(anchor) {
    const p = popEl(), r = anchor.getBoundingClientRect(), b = p.getBoundingClientRect();
    let top = r.bottom + 8, left = r.left + r.width / 2 - b.width / 2;
    if (top + b.height > innerHeight - 8 && r.top - b.height - 8 > 8) top = r.top - b.height - 8;
    p.style.top = `${Math.max(8, Math.min(top, innerHeight - b.height - 8))}px`;
    p.style.left = `${Math.max(8, Math.min(left, innerWidth - b.width - 8))}px`;
  }
  function contentFor(anchor) {
    if (anchor.dataset.uTip) return { tip: true, el: document.createTextNode(anchor.dataset.uTip) };
    for (const pr of providers) if (anchor.matches(pr.selector)) return { tip: false, el: pr.render(anchor) };
    return null;
  }
  function openPop(anchor, content) {
    const c = content ? { tip: false, el: content } : contentFor(anchor);
    if (!c || !c.el) return;
    const p = popEl();
    p.className = c.tip ? 'u-pop u-tip' : 'u-pop';
    p.setAttribute('role', c.tip ? 'tooltip' : 'dialog');
    p.replaceChildren(c.el);
    try { if (!p.matches(':popover-open')) p.showPopover(); } catch { return; }
    placePop(anchor);
    if (c.tip) anchor.setAttribute('aria-describedby', 'u-pop');
    popFor = anchor;
  }
  function closePop() {
    clearTimeout(openTimer); clearTimeout(closeTimer);
    if (!pop || !popFor) return;
    try { pop.hidePopover(); } catch {}
    popFor.removeAttribute('aria-describedby');
    popFor = null;
    warmUntil = Date.now() + 400;
  }
  function scheduleClose() { clearTimeout(closeTimer); closeTimer = setTimeout(closePop, 200); }
  const anchorOf = (e) => e.target.closest && e.target.closest('[data-u-tip], ' + (providers.map((p) => p.selector).join(', ') || '[data-u-card]'));
  const card = {
    provide(selector, render) { providers.push({ selector, render }); },
    toggle(anchor, content) { if (popFor === anchor) closePop(); else { clearTimeout(openTimer); openPop(anchor, typeof content === 'function' ? content() : content); } },
    close: closePop,
  };

  // ---------------------------------------------------------------- keyboard: shortcuts registry and list navigation
  // U.keys.add('j', fn, {label: 'Next creator', single: true}). Single-key shortcuts obey the "Single-key shortcuts"
  // preference (WCAG 2.1.4) and never fire while typing.
  const keymap = new Map();
  const typing = (t) => t && t.closest && t.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]');
  const keys = {
    add(combo, run, meta = {}) { keymap.set(combo.toLowerCase(), { combo, run, ...meta, single: !/[+]/.test(combo) && combo.length <= 2 }); },
    list: () => [...keymap.values()].filter((k) => k.label),
  };
  function comboOf(e) {
    const parts = [];
    if (e.metaKey || e.ctrlKey) parts.push('mod');
    if (e.altKey) parts.push('alt');
    if (e.shiftKey && e.key.length > 1) parts.push('shift');
    parts.push(e.key.toLowerCase());
    return parts.join('+');
  }
  function rovingList(container, { item = '[data-u-item]', onOpen, onMove, onClose } = {}) {
    const items = () => $$(item, container);
    const focusAt = (i) => {
      const all = items();
      if (!all.length) return null;
      const n = Math.max(0, Math.min(all.length - 1, i));
      all.forEach((el, k) => (el.tabIndex = k === n ? 0 : -1));
      all[n].focus({ preventScroll: true });
      all[n].scrollIntoView({ block: 'nearest' });       // instant: keyboard moves never animate
      onMove && onMove(all[n], n);
      return all[n];
    };
    const current = () => items().indexOf(document.activeElement && document.activeElement.closest(item));
    container.addEventListener('keydown', (e) => {
      if (typing(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
      const own = e.target.closest(item);
      // A button or link inside a row handles its own Enter/Space; the list only acts on the row itself.
      if ((e.key === 'Enter' || e.key === ' ') && own && e.target !== own && e.target.closest('button, a, input, select, textarea, [role="button"]')) return;
      const i = current(), single = prefs.keys;
      if (e.key === 'ArrowDown' || (single && e.key === 'j')) { e.preventDefault(); focusAt(i + 1); }
      else if (e.key === 'ArrowUp' || (single && e.key === 'k')) { e.preventDefault(); focusAt(i < 0 ? 0 : i - 1); }
      else if (e.key === 'Home') { e.preventDefault(); focusAt(0); }
      else if (e.key === 'End') { e.preventDefault(); focusAt(items().length - 1); }
      else if (e.key === 'Enter' && i >= 0 && onOpen) { e.preventDefault(); onOpen(items()[i], i); }
      else if (e.key === 'Escape' && onClose) onClose();
    });
    container.addEventListener('focusin', (e) => { const el = e.target.closest(item); if (el) items().forEach((x) => (x.tabIndex = x === el ? 0 : -1)); });
    const refresh = () => { const all = items(); if (!all.length) return; const sel = all.findIndex((x) => x.tabIndex === 0); all.forEach((x, k) => (x.tabIndex = k === (sel < 0 ? 0 : sel) ? 0 : -1)); };
    return { focusAt, refresh, current };
  }

  // ---------------------------------------------------------------- command palette (Ctrl/Cmd+K); opens instantly
  const commands = new Map();
  let cmdk;
  function fuzzy(q, text) {
    if (!q) return 1;
    text = text.toLowerCase(); q = q.toLowerCase();
    if (text.includes(q)) return 100 - text.indexOf(q);
    let last = -1, s = 0;
    for (const ch of q) { const j = text.indexOf(ch, last + 1); if (j < 0) return 0; s += j === last + 1 ? 3 : 1; last = j; }
    return s;
  }
  function kbdEls(combo) {
    if (!combo) return null;
    const mac = /Mac|iPhone|iPad/.test(navigator.platform);
    return h('span', { class: 'u-row', style: { '--gap': '3px', 'margin-inline-start': 'auto' } },
      ...combo.split('+').map((k) => h('kbd', { class: 'u-kbd' }, k === 'mod' ? (mac ? '⌘' : 'Ctrl') : k.length === 1 ? k.toUpperCase() : k)));
  }
  function openPalette(prefill = '') {
    if (!cmdk) {
      const input = h('input', { type: 'text', placeholder: 'Search or run a command…', 'aria-label': 'Command', role: 'combobox', 'aria-expanded': 'true',
                                 'aria-controls': 'u-cmdk-list', 'aria-autocomplete': 'list', autocomplete: 'off', spellcheck: 'false' });
      const list = h('ul', { id: 'u-cmdk-list', role: 'listbox', 'aria-label': 'Commands' });
      cmdk = h('dialog', { class: 'u-cmdk', 'aria-label': 'Command palette' }, input, list,
        h('div', { class: 'foot' }, h('span', {}, h('kbd', { class: 'u-kbd' }, '↑'), ' ', h('kbd', { class: 'u-kbd' }, '↓'), ' move'),
          h('span', {}, h('kbd', { class: 'u-kbd' }, '↵'), ' run'), h('span', {}, h('kbd', { class: 'u-kbd' }, 'Esc'), ' close')));
      document.body.append(cmdk);
      let sel = 0, shown = [];
      const draw = () => {
        const q = input.value.trim();
        const pool = [...commands.values()].filter((c) => !c.when || c.when()).flatMap((c) => (c.search ? c.search(q) || [] : [c]));
        shown = pool.map((c) => ({ c, s: Math.max(fuzzy(q, c.label), fuzzy(q, c.keywords || '') * 0.8) })).filter((x) => x.s > 0)
          .sort((a, b) => (b.c.pin ? 1 : 0) - (a.c.pin ? 1 : 0) || b.s - a.s).slice(0, 50).map((x) => x.c);
        sel = Math.min(sel, Math.max(0, shown.length - 1));
        const groups = [...new Set(shown.map((c) => c.group || 'Actions'))];
        list.replaceChildren(...(shown.length ? groups.flatMap((g) => [h('li', { class: 'group', role: 'presentation' }, g),
          ...shown.filter((c) => (c.group || 'Actions') === g).map((c) => {
            const k = shown.indexOf(c);
            return h('li', { role: 'option', id: `u-cmd-${k}`, 'aria-selected': String(k === sel), onpointermove: () => { if (sel !== k) { sel = k; draw(); } },
                             onclick: () => run(k) }, h('span', {}, c.label), c.hint ? h('small', {}, c.hint) : null, kbdEls(c.shortcut));
          })]) : [h('li', { class: 'group', role: 'presentation' }, q ? `Nothing matches “${q}”` : 'No commands')]));
        input.setAttribute('aria-activedescendant', shown.length ? `u-cmd-${sel}` : '');
        const on = document.getElementById(`u-cmd-${sel}`);
        on && on.scrollIntoView({ block: 'nearest' });
      };
      const run = (k) => { const c = shown[k]; if (!c) return; const q = input.value.trim(); cmdk.close(); c.run(q); };
      input.addEventListener('input', () => { sel = 0; draw(); });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(shown.length - 1, sel + 1); draw(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(0, sel - 1); draw(); }
        else if (e.key === 'Enter') { e.preventDefault(); run(sel); }
      });
      cmdk.addEventListener('click', (e) => { if (e.target === cmdk) cmdk.close(); });
      cmdk.draw = draw; cmdk.input = input;
    }
    cmdk.input.value = prefill;
    cmdk.showModal();
    cmdk.draw();
    cmdk.input.focus();
  }
  const palette = { add(c) { commands.set(c.id || c.label, c); }, remove(id) { commands.delete(id); }, open: openPalette };

  // ---------------------------------------------------------------- settings and shortcut sheet
  let settingsDlg, helpDlg;
  function settings() {
    if (!settingsDlg) {
      const radio = (name, value, label) => h('label', {}, h('input', { type: 'radio', name, value }), label);
      const body = h('div', { class: 'u-dialog-body' }, h('h2', { class: 'u-h3' }, 'Display'),
        h('div', { class: 'u-field' }, h('span', { class: 'u-label' }, 'Theme'),
          h('div', { class: 'u-seg', 'data-u-seg': '', role: 'radiogroup', 'aria-label': 'Theme' }, radio('u-theme', 'light', 'Light'), radio('u-theme', 'dark', 'Dark'), radio('u-theme', 'system', 'System'))),
        h('div', { class: 'u-field' }, h('span', { class: 'u-label' }, 'Row density'),
          h('div', { class: 'u-seg', 'data-u-seg': '', role: 'radiogroup', 'aria-label': 'Row density' }, radio('u-density', 'compact', 'Compact'), radio('u-density', 'comfortable', 'Comfortable'), radio('u-density', 'roomy', 'Roomy'))),
        h('label', { class: 'u-switch' }, h('input', { type: 'checkbox', name: 'u-motion' }), 'Reduce motion'),
        h('label', { class: 'u-switch' }, h('input', { type: 'checkbox', name: 'u-keys' }), 'Single-key shortcuts (j, k, /, ?)'),
        h('label', { class: 'u-switch' }, h('input', { type: 'checkbox', name: 'u-present' }), 'Present mode (projectors: stronger lines, larger text)'));
      settingsDlg = h('dialog', { class: 'u-dialog', 'aria-label': 'Display settings' }, body,
        h('div', { class: 'u-dialog-foot' }, h('button', { class: 'u-btn u-btn-primary', type: 'button', onclick: () => settingsDlg.close() }, 'Done')));
      settingsDlg.addEventListener('change', (e) => {
        const t = e.target;
        if (t.name === 'u-theme') setPref('theme', t.value);
        if (t.name === 'u-density') setPref('density', t.value);
        if (t.name === 'u-motion') setPref('motion', t.checked ? 'reduce' : 'system');
        if (t.name === 'u-keys') setPref('keys', t.checked);
        if (t.name === 'u-present') setPref('present', t.checked);
      });
      settingsDlg.addEventListener('click', (e) => { if (e.target === settingsDlg) settingsDlg.close(); });
      document.body.append(settingsDlg);
    }
    $$(`[name="u-theme"]`, settingsDlg).forEach((r) => (r.checked = r.value === prefs.theme));
    $$(`[name="u-density"]`, settingsDlg).forEach((r) => (r.checked = r.value === prefs.density));
    $('[name="u-motion"]', settingsDlg).checked = prefs.motion === 'reduce';
    $('[name="u-keys"]', settingsDlg).checked = prefs.keys;
    $('[name="u-present"]', settingsDlg).checked = prefs.present;
    settingsDlg.showModal();
    scan(settingsDlg);
  }
  function shortcutSheet() {
    if (!helpDlg) {
      helpDlg = h('dialog', { class: 'u-dialog', 'aria-label': 'Keyboard shortcuts' });
      helpDlg.addEventListener('click', (e) => { if (e.target === helpDlg) helpDlg.close(); });
      document.body.append(helpDlg);
    }
    const rows = [{ combo: 'mod+k', label: 'Command palette' }, ...keys.list(), ...[...commands.values()].filter((c) => c.shortcut && !keymap.has(c.shortcut)).map((c) => ({ combo: c.shortcut, label: c.label }))];
    helpDlg.replaceChildren(h('div', { class: 'u-dialog-body' }, h('h2', { class: 'u-h3' }, 'Keyboard shortcuts'),
      h('div', { class: 'u-stack', style: { '--gap': '6px' } }, ...rows.map((k) => h('div', { class: 'u-row' }, h('span', { class: 'u-grow' }, k.label), kbdEls(k.combo)))),
      prefs.keys ? null : h('p', { class: 'u-hint' }, 'Single-key shortcuts are off. Turn them on in Display settings.')),
      h('div', { class: 'u-dialog-foot' }, h('button', { class: 'u-btn', type: 'button', onclick: () => { helpDlg.close(); settings(); } }, 'Display settings'),
        h('button', { class: 'u-btn u-btn-primary', type: 'button', onclick: () => helpDlg.close() }, 'Done')));
    helpDlg.showModal();
  }

  // ---------------------------------------------------------------- typed hero text
  function type(el, words, { speed = 65, hold = 1700 } = {}) {
    if (reduced()) { el.textContent = words[0]; return () => {}; }
    let alive = true, w = 0;
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    (async () => {
      while (alive) {
        const word = words[w++ % words.length];
        for (let i = 1; i <= word.length && alive; i++) { el.textContent = word.slice(0, i); await wait(speed); }
        await wait(hold);
        for (let i = word.length; i >= 0 && alive; i--) { el.textContent = word.slice(0, i); await wait(speed / 2); }
      }
    })();
    return () => { alive = false; };
  }

  // ---------------------------------------------------------------- wiring
  function scan(rootEl = document) {
    for (const el of $$('[data-u-count], .u-reveal', rootEl)) { if (el.dataset.uSeen) continue; el.dataset.uSeen = '1'; io.observe(el); }
    for (const el of $$('[data-u-seg]', rootEl)) seg(el);
  }
  function boot() {
    scan();
    new MutationObserver((muts) => { for (const m of muts) for (const n of m.addedNodes) if (n.nodeType === 1) scan(n); })
      .observe(document.body, { childList: true, subtree: true });
    if (matchMedia('(hover: hover) and (pointer: fine)').matches) {
      document.addEventListener('pointermove', (e) => {
        const el = e.target.closest && e.target.closest('.u-spot');
        if (!el) return;
        const r = el.getBoundingClientRect();
        el.style.setProperty('--mx', `${e.clientX - r.left}px`);
        el.style.setProperty('--my', `${e.clientY - r.top}px`);
      }, { passive: true });
    }
    document.addEventListener('pointerover', (e) => {
      const a = anchorOf(e);
      if (!a || a === popFor) { if (a) clearTimeout(closeTimer); return; }
      clearTimeout(openTimer); clearTimeout(closeTimer);
      openTimer = setTimeout(() => openPop(a), Date.now() < warmUntil || popFor ? 0 : 350);
    });
    document.addEventListener('pointerout', (e) => {
      const a = anchorOf(e);
      if (!a || (e.relatedTarget && (a.contains(e.relatedTarget) || (pop && pop.contains(e.relatedTarget))))) return;
      clearTimeout(openTimer);
      if (popFor) scheduleClose();
    });
    document.addEventListener('focusin', (e) => { const a = anchorOf(e); if (a && e.target.matches(':focus-visible')) openPop(a); });
    document.addEventListener('focusout', (e) => { if (anchorOf(e) && !(pop && pop.contains(e.relatedTarget))) scheduleClose(); });
    addEventListener('scroll', (e) => {          // the page scrolling (or a panel holding the anchor), not any inner panel
      if (!popFor) return;
      const t = e.target;
      if (t === document || t === document.documentElement || t === document.body || (t.contains && t.contains(popFor))) closePop();
    }, { passive: true, capture: true });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && popFor) { closePop(); return; }
      const combo = comboOf(e);
      if (combo === 'mod+k' && commands.size) { e.preventDefault(); if (cmdk && cmdk.open) cmdk.close(); else openPalette(); return; }
      if (typing(e.target) || document.querySelector('dialog[open]')) return;
      if (combo === '?' && prefs.keys) { e.preventDefault(); shortcutSheet(); return; }
      const k = keymap.get(combo);
      if (k && (!k.single || prefs.keys) && (!k.when || k.when())) { e.preventDefault(); k.run(e); }
    });
    palette.add({ id: 'u-settings', label: 'Display settings', hint: 'Theme, density, motion, present mode', group: 'Settings', run: settings });
    palette.add({ id: 'u-present', label: 'Toggle present mode', hint: 'For projectors', group: 'Settings', run: () => { setPref('present', !prefs.present); toast(prefs.present ? 'Present mode on' : 'Present mode off', { kind: 'ok' }); } });
    palette.add({ id: 'u-theme', label: 'Toggle dark theme', group: 'Settings', run: () => setPref('theme', prefs.theme === 'dark' ? 'light' : 'dark') });
    palette.add({ id: 'u-help', label: 'Keyboard shortcuts', group: 'Settings', shortcut: '?', run: shortcutSheet });
  }
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', boot) : boot();

  window.U = { h, svg, $, $$, fmt, clock, ago, date, transition, toast, optimistic, copy, countUp, receipt, quoteEl, scoreRing, band, meter, why, sparkline,
               seg, tabs, card, keys, rovingList, palette, settings, shortcutSheet, type, scan, reduced, get prefs() { return { ...prefs }; }, setPref, PLATFORM, KIND };
})();
