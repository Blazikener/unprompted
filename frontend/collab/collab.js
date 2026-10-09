/* Shared by /collab/ (one collab: compose or answer) and /collabs (the inbox). window.C. */
(() => {
  const { h } = U;
  const usd = (n) => `$${U.fmt(n)}`;
  const STATUS = { sent: ['u-pill-warn', 'Waiting'], countered: ['u-pill-warn', 'Counter-offer'], accepted: ['u-pill-ok', 'Booked'],
                   declined: ['', 'Declined'], withdrawn: ['', 'Withdrawn'] };

  async function call(path, body) {
    const res = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { const e = new Error(data.error || 'Something went wrong.'); e.status = res.status; e.data = data; throw e; }
    return data;
  }

  async function pending(btn, work) {
    if (btn.getAttribute('aria-busy') === 'true') return;
    btn.setAttribute('aria-busy', 'true');
    try { return await work(); } finally { if (btn.isConnected) btn.removeAttribute('aria-busy'); }
  }

  const field = (label, input, hint) => h('label', { class: 'u-field' }, h('span', { class: 'u-label' }, label), input, hint ? h('small', { class: 'u-hint' }, hint) : null);

  // The terms of a future collab: what gets made, when it posts, the budget and a brief. Returns { el, read() }.
  function termsForm(init = {}) {
    const deliverables = h('input', { class: 'u-input', name: 'deliverables', required: true, maxlength: 200, value: init.deliverables || '',
      placeholder: '2 TikToks + 1 story' });
    const timing = h('input', { class: 'u-input', name: 'timing', required: true, maxlength: 120, value: init.timing || '', placeholder: 'Posting in June' });
    const budget = h('input', { class: 'u-input', name: 'budget', required: true, type: 'number', min: 50, step: 1, inputmode: 'numeric',
      value: init.budgetUsd || '', placeholder: '2600' });
    const brief = h('textarea', { class: 'u-textarea', name: 'brief', rows: 3, maxlength: 1000, placeholder: 'Your style, your call. Talking points, dos and don’ts.' },
      init.brief || '');
    const presets = h('div', { class: 'c-presets', role: 'group', 'aria-label': 'Common deliverables' },
      ...['1 TikTok', '2 TikToks + 1 story', '1 Reel + 3 stories', 'A 60s YouTube integration'].map((p) =>
        h('button', { type: 'button', class: 'u-btn u-btn-ghost u-btn-sm', onclick: () => { deliverables.value = p; deliverables.focus(); } }, p)));
    const el = h('div', { class: 'c-terms' },
      field('What gets made', deliverables), presets,
      h('div', { class: 'c-two' }, field('When it posts', timing), field('Budget (USD)', h('div', { class: 'c-money' }, h('span', { 'aria-hidden': 'true' }, '$'), budget))),
      field('Brief', brief, 'Optional. Creators do best in their own voice.'));
    return { el, read: () => ({ deliverables: deliverables.value.trim(), timing: timing.value.trim(), budgetUsd: Number.parseInt(budget.value, 10) || 0,
      brief: brief.value.trim() }) };
  }

  // The receipt behind a collab: proof the creator already likes the brand, not the thing being paid for.
  function proof(c) {
    if (!c.proof) return null;
    return h('div', { class: 'c-proof' }, h('span', { class: 'u-eyebrow' }, 'Why this collab: they already said it'),
      U.receipt({ ...c.proof, handle: c.handle, platform: c.platform }, { size: 'lg' }));
  }

  function termsList(c) {
    const rows = [['Makes', c.deliverables], ['Posts', c.timing], ['Budget', usd(c.budgetUsd)], ['Brief', c.brief || '—']];
    if (c.note) rows.push([c.status === 'countered' ? 'Counter note' : 'Note', c.note]);
    if (c.declineReason) rows.push(['Reason', c.declineReason]);
    return h('dl', { class: 'c-dl' }, ...rows.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
  }

  const pill = (c) => {
    const [cls, label] = c.yourTurn ? ['u-pill-warn', 'Your turn'] : STATUS[c.status];
    return h('span', { class: `u-pill ${cls}` }, label);
  };

  // What the other side sees as the headline of this collab.
  function headline(c) {
    if (c.side === 'creator') return c.origin === 'brand' ? `${c.brand} wants to collab with you` : `Your pitch to ${c.brand}`;
    return c.origin === 'brand' ? `Your invite to @${c.handle}` : `@${c.handle} pitched a collab`;
  }

  function status(c) {
    const other = c.side === 'creator' ? c.brand : `@${c.handle}`;
    if (c.status === 'accepted') return `Booked at ${usd(c.budgetUsd)}. Agree the details directly; nothing is charged here.`;
    if (c.status === 'declined') return c.lastBy === c.side ? 'You declined.' : `${other} declined.`;
    if (c.status === 'withdrawn') return c.lastBy === c.side ? 'You withdrew this.' : `${other} withdrew this.`;
    if (c.yourTurn) return c.status === 'countered' ? `${other} countered at ${usd(c.budgetUsd)}. Accept, counter or decline.` : 'Accept, counter or decline.';
    return `Waiting for ${other} to answer.`;
  }

  // A small modal that waits for the server before closing; errors stay inside it.
  function dialog({ title, lead, body = [], confirm, danger, run }) {
    const err = h('p', { class: 'u-error', role: 'alert' });
    const ok = h('button', { class: `u-btn ${danger ? 'u-btn-danger' : 'u-btn-primary'}`, type: 'submit' }, confirm);
    const cancel = h('button', { class: 'u-btn', type: 'button' }, 'Cancel');
    const id = `dlg-${Math.random().toString(36).slice(2, 8)}`;
    const form = h('form', { onsubmit: async (e) => {
      e.preventDefault();
      err.textContent = '';
      try { await pending(ok, run); dlg.close(); } catch (ex) { err.textContent = ex.message; }
    } }, h('div', { class: 'u-dialog-body' }, h('h2', { class: 'u-h3', id }, title), lead ? h('p', { class: 'u-muted' }, lead) : null, ...body, err),
      h('div', { class: 'u-dialog-foot' }, cancel, ok));
    const dlg = h('dialog', { class: 'u-dialog', 'aria-labelledby': id }, form);
    cancel.addEventListener('click', () => dlg.close());
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener('close', () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
    return dlg;
  }

  // Accept / counter / decline / withdraw buttons for one collab; `post(body)` sends the answer and `done(view)` re-renders.
  function actions(c, post, done) {
    const said = { accept: 'Booked', counter: 'Counter-offer sent', decline: 'Declined', withdraw: 'Withdrawn' };
    const send = async (body) => { const v = await post(body); U.toast(said[body.action], { kind: body.action === 'accept' ? 'ok' : 'info' }); done(v); };
    const out = [];
    if (c.yourTurn) {
      out.push(h('button', { class: 'u-btn u-btn-primary', type: 'button', onclick: () => dialog({ title: `Book it at ${usd(c.budgetUsd)}?`,
        lead: `${c.deliverables}, ${c.timing}.`, confirm: 'Accept', run: () => send({ action: 'accept' }) }) }, `Accept ${usd(c.budgetUsd)}`));
      out.push(h('button', { class: 'u-btn', type: 'button', onclick: () => {
        const amount = h('input', { class: 'u-input', type: 'number', min: 50, step: 1, required: true, value: c.budgetUsd, inputmode: 'numeric' });
        const note = h('textarea', { class: 'u-textarea', rows: 2, maxlength: 500, placeholder: 'e.g. Add a Reel and I’m in.' });
        dialog({ title: 'Counter-offer', lead: 'Set your budget. The other side can accept, counter or decline.', confirm: 'Send counter',
          body: [field('Budget (USD)', amount), field('Note', note)],
          run: () => send({ action: 'counter', budgetUsd: Number.parseInt(amount.value, 10) || 0, note: note.value.trim() }) });
      } }, 'Counter'));
      out.push(h('button', { class: 'u-btn u-btn-ghost', type: 'button', onclick: () => {
        const reason = h('input', { class: 'u-input', maxlength: 300, placeholder: 'Optional: not the right fit right now' });
        dialog({ title: 'Decline this collab?', confirm: 'Decline', danger: true, body: [field('Reason', reason)],
          run: () => send({ action: 'decline', reason: reason.value.trim() }) });
      } }, 'Decline'));
    } else if (c.canWithdraw) {
      out.push(h('button', { class: 'u-btn u-btn-ghost', type: 'button', onclick: () => dialog({ title: 'Withdraw this?', confirm: 'Withdraw', danger: true,
        run: () => send({ action: 'withdraw' }) }) }, 'Withdraw'));
    }
    return out.length ? h('div', { class: 'c-actions' }, ...out) : null;
  }

  window.C = { call, pending, field, termsForm, proof, termsList, pill, headline, status, actions, dialog, usd };
})();
