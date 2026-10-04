/* Landing motion layer, opt-in by data attribute. Load after ui.js; no dependencies.
   [data-split]     headline revealed word by word when it enters the viewport (text stays real text)
   [data-reveal]    rises in once; data-reveal="2" staggers siblings via --d
   [data-story]     a tall section with a sticky stage: sets --p (0..1) and data-step (0..steps-1) from scroll
   [data-parallax]  drifts with scroll by the given factor (e.g. "0.15")
   [data-magnetic]  leans toward a fine pointer, a few pixels
   [data-tilt]      card tilts toward the pointer and gets a spotlight at --mx/--my
   [data-progress]  the page's scroll ruler (one per page)
   Everything here is decoration: with motion reduced (OS or the in-app setting) content is shown at once and nothing moves. */
(() => {
  const root = document.documentElement;
  const mq = matchMedia('(prefers-reduced-motion: reduce)');
  const fine = matchMedia('(hover: hover) and (pointer: fine)');
  const reduced = () => mq.matches || root.dataset.motion === 'reduce';
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  // ---- split text into words, keeping inline elements (em, mark, bdi, br) intact
  function split(el) {
    if (el.dataset.splitDone) return;
    el.dataset.splitDone = '1';
    let i = 0;
    const walk = (node) => {
      for (const child of [...node.childNodes]) {
        if (child.nodeType === 3) {
          const parts = child.textContent.split(/(\s+)/);
          const frag = document.createDocumentFragment();
          for (const p of parts) {
            if (!p) continue;
            if (/^\s+$/.test(p)) { frag.append(' '); continue; }
            const w = document.createElement('span');
            w.className = 'm-w';
            const inner = document.createElement('span');
            inner.className = 'm-wi';
            inner.style.setProperty('--i', i++);
            inner.textContent = p;
            w.append(inner);
            frag.append(w);
          }
          child.replaceWith(frag);
        } else if (child.nodeType === 1 && child.tagName !== 'BR') {
          walk(child);
        }
      }
    };
    walk(el);
  }

  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { e.target.classList.add('is-in'); io.unobserve(e.target); }
  }, { rootMargin: '0px 0px -8% 0px', threshold: 0.12 });

  function scan(scope = document) {
    $$('[data-split]', scope).forEach((el) => { split(el); reduced() ? el.classList.add('is-in') : io.observe(el); });
    $$('[data-reveal]', scope).forEach((el) => {
      if (el.dataset.revealDone) return;
      el.dataset.revealDone = '1';
      if (el.dataset.reveal === '2') [...el.children].forEach((c, k) => c.style.setProperty('--d', k));
      reduced() ? el.classList.add('is-in') : io.observe(el);
    });
  }

  // ---- scroll-linked: stories, parallax, the progress ruler (one rAF per scroll frame)
  let ticking = false;
  const stories = () => $$('[data-story]');
  const parallax = () => $$('[data-parallax]');
  const ruler = document.querySelector('[data-progress]');
  const rulerPct = ruler?.querySelector('[data-progress-pct]');
  function onScroll() {
    ticking = false;
    const vh = innerHeight;
    for (const el of stories()) {
      const r = el.getBoundingClientRect();
      const span = Math.max(1, r.height - vh);
      const p = Math.min(1, Math.max(0, -r.top / span));
      const steps = +el.dataset.story || 3;
      el.style.setProperty('--p', p.toFixed(4));
      const step = String(Math.min(steps - 1, Math.floor(p * steps * 0.999)));
      if (el.dataset.step !== step) el.dataset.step = step;
    }
    if (!reduced()) {
      for (const el of parallax()) {
        const r = el.getBoundingClientRect();
        if (r.bottom < -vh || r.top > vh * 2) continue;
        const k = parseFloat(el.dataset.parallax) || 0.1;
        el.style.translate = `0 ${((r.top + r.height / 2 - vh / 2) * -k).toFixed(1)}px`;
      }
    }
    if (ruler) {
      const max = Math.max(1, document.documentElement.scrollHeight - vh);
      const p = Math.min(1, Math.max(0, scrollY / max));
      ruler.style.setProperty('--p', p.toFixed(4));
      if (rulerPct) rulerPct.textContent = `${String(Math.round(p * 100)).padStart(3, '0')}%`;
    }
  }
  const requestScroll = () => { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } };
  addEventListener('scroll', requestScroll, { passive: true });
  addEventListener('resize', requestScroll, { passive: true });

  // ---- pointer: magnetic buttons, tilt cards, the trailing square cursor (Altitude)
  let cursor = null, cx = -100, cy = -100, tx = -100, ty = -100, craf = 0;
  function cursorLoop() {
    if (!cursor) { craf = 0; return; }
    cx += (tx - cx) * 0.2; cy += (ty - cy) * 0.2;
    cursor.style.transform = `translate3d(${cx.toFixed(1)}px, ${cy.toFixed(1)}px, 0)`;
    craf = Math.abs(tx - cx) + Math.abs(ty - cy) > 0.3 ? requestAnimationFrame(cursorLoop) : 0;
  }
  function ensureCursor() {
    const want = fine.matches && !reduced() && document.body?.dataset.cursor !== 'off';
    if (want && !cursor) {
      cursor = document.createElement('div');
      cursor.className = 'm-cursor is-gone';
      cursor.setAttribute('aria-hidden', 'true');
      document.body.append(cursor);
    }
    if (!want && cursor) { cancelAnimationFrame(craf); craf = 0; cursor.remove(); cursor = null; }
  }
  addEventListener('pointermove', (e) => {
    if (e.pointerType !== 'mouse') return;
    if (cursor) {
      tx = e.clientX; ty = e.clientY;
      const t = e.target.closest?.('a, button, input, select, textarea, label, [role="button"], [data-tilt]');
      cursor.classList.toggle('is-hover', !!t);
      cursor.classList.toggle('is-text', !!e.target.closest?.('input, textarea'));
      if (!craf) craf = requestAnimationFrame(cursorLoop);
    }
    if (reduced()) return;
    const mag = e.target.closest?.('[data-magnetic]');
    $$('[data-magnetic].is-pulled').forEach((el) => { if (el !== mag) { el.classList.remove('is-pulled'); el.style.translate = ''; } });
    if (mag) {
      const r = mag.getBoundingClientRect();
      const dx = (e.clientX - (r.left + r.width / 2)) / r.width, dy = (e.clientY - (r.top + r.height / 2)) / r.height;
      mag.classList.add('is-pulled');
      mag.style.translate = `${(dx * 8).toFixed(1)}px ${(dy * 6).toFixed(1)}px`;
    }
    const tilt = e.target.closest?.('[data-tilt]');
    if (tilt) {
      const r = tilt.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
      tilt.style.setProperty('--mx', `${(x * 100).toFixed(1)}%`);
      tilt.style.setProperty('--my', `${(y * 100).toFixed(1)}%`);
      tilt.style.setProperty('--rx', `${((0.5 - y) * 6).toFixed(2)}deg`);
      tilt.style.setProperty('--ry', `${((x - 0.5) * 8).toFixed(2)}deg`);
    }
  }, { passive: true });
  document.addEventListener('pointerout', (e) => {
    const tilt = e.target.closest?.('[data-tilt]');
    if (tilt && !tilt.contains(e.relatedTarget)) { tilt.style.setProperty('--rx', '0deg'); tilt.style.setProperty('--ry', '0deg'); }
    if (!e.relatedTarget && cursor) cursor.classList.add('is-gone'); else cursor?.classList.remove('is-gone');
  });

  function refresh() {
    ensureCursor();
    if (reduced()) $$('[data-split], [data-reveal]').forEach((el) => el.classList.add('is-in'));
    requestScroll();
  }
  document.addEventListener('u:prefs', refresh);
  mq.addEventListener('change', refresh);
  fine.addEventListener('change', refresh);

  const boot = () => { root.classList.add('m-ready'); scan(); refresh(); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
  window.M = { scan, refresh, reduced };
})();
