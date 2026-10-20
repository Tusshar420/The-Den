// Touch/pointer interactions from the prototype, as framework-free helpers.
// Each returns a cleanup function. All thresholds match the prototype exactly.

// ------------------------------------------------------------------ page carousel (3D)
// Swipe left → next tab page, swipe right → previous. On a project detail page, swipe right = back.
// wrapper: the .pager element that holds the current page's content (NOT the sticky header).
// getNeighbors(): { prev: fn | null, next: fn | null }  — each fn navigates + re-renders.
export function pageSwipe(wrapper, getNeighbors, { lockRatio = 1.3, commit = 70, edgeResistance = 0.25 } = {}) {
  let info = null, anim = false, justSwiped = false;
  const W = () => wrapper.parentElement.clientWidth || 390;
  const apply = (x, live) => {
    const r = x / W();
    wrapper.classList.toggle('is-dragging', live);
    if (x < 0) wrapper.style.transformOrigin = '100% 50%'; else if (x > 0) wrapper.style.transformOrigin = '0% 50%';
    wrapper.style.transform = x ? `perspective(1100px) translateX(${x}px) rotateY(${(r * 34).toFixed(2)}deg) scale(${(1 - Math.abs(r) * 0.12).toFixed(3)})` : 'none';
    wrapper.style.opacity = String(1 - Math.min(0.6, Math.abs(r) * 0.7));
  };
  const down = e => {
    if (anim || e.button || e.clientX <= 24) return;   // the left screen edge opens the sidebar drawer
    if (e.target.closest('[data-noswipe],input,textarea,label,.handle,.swipe__card')) return;
    info = { x0: e.clientX, y0: e.clientY, lock: null };
  };
  const move = e => {
    if (!info) return;
    const dx = e.clientX - info.x0, dy = e.clientY - info.y0;
    if (!info.lock && (Math.abs(dx) > 10 || Math.abs(dy) > 10)) info.lock = Math.abs(dx) > Math.abs(dy) * lockRatio ? 'x' : 'y';
    if (info.lock === 'y') { info = null; return; }
    if (info.lock === 'x') { const n = getNeighbors(); const edge = (dx < 0 && !n.next) || (dx > 0 && !n.prev); apply(edge ? dx * edgeResistance : dx, true); info.dx = dx; }
  };
  const up = () => {
    if (!info) return;
    const { lock, dx = 0 } = info; info = null;
    if (lock !== 'x') return;
    justSwiped = true; setTimeout(() => (justSwiped = false), 80);
    const n = getNeighbors();
    const go = dx < -commit ? n.next : dx > commit ? n.prev : null;
    if (!go) { apply(0, false); return; }
    const dir = dx < 0 ? -1 : 1;
    anim = true;
    apply(dir * W(), false);                      // fling current page out
    setTimeout(() => {
      go();                                       // swap content (skeleton shows first, see README › Loading)
      apply(-dir * W() * 0.7, true);              // place new page on the opposite side
      requestAnimationFrame(() => requestAnimationFrame(() => { apply(0, false); setTimeout(() => (anim = false), 280); }));
    }, 230);
  };
  const clickCapture = e => { if (justSwiped) { e.stopPropagation(); e.preventDefault(); } };
  wrapper.addEventListener('pointerdown', down);
  wrapper.addEventListener('click', clickCapture, true);
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
  window.addEventListener('pointercancel', up);
  return () => {
    wrapper.removeEventListener('pointerdown', down); wrapper.removeEventListener('click', clickCapture, true);
    window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', up);
  };
}

// ------------------------------------------------------------------ swipe to approve / deny
// card: .swipe__card; reveal: .swipe__reveal (its background + labels change with direction).
export function swipeDecision(card, reveal, { onApprove, onDeny, threshold = 110 }) {
  let x0 = null, x = 0;
  const paint = () => {
    card.style.transform = x ? `perspective(900px) translateX(${x}px) rotateY(${(x / 14).toFixed(1)}deg) rotate(${(x / 40).toFixed(1)}deg)` : 'none';
    reveal.style.background = x > 20 ? 'var(--wait)' : x < -20 ? 'var(--err)' : 'transparent';
    reveal.children[0].textContent = x > 20 ? '✓ Approve' : '';
    reveal.children[1].textContent = x < -20 ? 'Deny ✕' : '';
  };
  const down = e => { if (e.target.closest('button')) return; x0 = e.clientX; card.classList.add('is-dragging'); };
  const move = e => { if (x0 == null) return; x = e.clientX - x0; paint(); };
  const up = () => {
    if (x0 == null) return; x0 = null; card.classList.remove('is-dragging');
    if (x > threshold) onApprove(); else if (x < -threshold) onDeny();
    x = 0; paint();
  };
  card.addEventListener('pointerdown', down);
  window.addEventListener('pointermove', move); window.addEventListener('pointerup', up); window.addEventListener('pointercancel', up);
  return () => { card.removeEventListener('pointerdown', down); window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', up); };
}

// ------------------------------------------------------------------ drag to reorder (Up next, Backlog)
// list: container of fixed-height rows; rows carry data-id; drag starts on .handle.
// onDrop(ids) receives the new id order → POST /projects/{id}/todos/reorder (backlog)
// or a queue reorder endpoint (see README › Backend gaps).
// group(id) may return the ids that travel with a dragged row (e.g. every selected row): they
// land together, in their current order, where the dragged row is dropped.
export function dragReorder(list, { rowHeight = () => parseFloat(getComputedStyle(list).getPropertyValue('--row-h')) || 56, onDrop, group }) {
  let drag = null;
  const rows = () => [...list.children].filter(r => r.dataset.id);
  const down = e => {
    const h = e.target.closest('.handle'); if (!h || list.querySelector('.is-open')) return;  // rows are only equal height while closed
    e.preventDefault(); e.stopPropagation();
    const row = h.closest('[data-id]'), all = rows();
    const g = group ? (group(row.dataset.id) || []).map(String) : [];
    const mates = g.length > 1 && g.includes(row.dataset.id) ? new Set(g) : null;
    drag = { row, from: all.indexOf(row), y0: e.clientY, dy: 0, all, mates };
    row.classList.add('is-dragging');
    if (mates) all.forEach(r => r !== row && mates.has(r.dataset.id) && r.classList.add('is-grouped'));
  };
  const target = () => Math.max(0, Math.min(drag.all.length - 1, drag.from + Math.round(drag.dy / rowHeight())));
  const move = e => {
    if (!drag) return;
    drag.dy = e.clientY - drag.y0;
    const to = target(), RH = rowHeight();
    drag.all.forEach((r, i) => {
      if (r === drag.row) r.style.transform = `translateY(${drag.dy}px)`;
      else if (drag.from < to && i > drag.from && i <= to) r.style.transform = `translateY(${-RH}px)`;
      else if (drag.from > to && i >= to && i < drag.from) r.style.transform = `translateY(${RH}px)`;
      else r.style.transform = '';
    });
  };
  const up = () => {
    if (!drag) return;
    const to = target(), from = drag.from, mates = drag.mates, ids = drag.all.map(r => r.dataset.id);
    drag.all.forEach(r => { r.style.transform = ''; r.classList.remove('is-grouped'); }); drag.row.classList.remove('is-dragging');
    drag = null;
    if (to === from) return;
    if (!mates) { const [moved] = ids.splice(from, 1); ids.splice(to, 0, moved); onDrop(ids); return; }
    // The group lands where the dragged row would have: count the other rows that stay ahead of it.
    const rest = ids.filter(id => !mates.has(id)), moving = ids.filter(id => mates.has(id));
    const ahead = ids.slice(0, to < from ? to : to + 1).filter(id => !mates.has(id)).length;
    rest.splice(ahead, 0, ...moving);
    if (rest.join() !== ids.join()) onDrop(rest);
  };
  list.addEventListener('pointerdown', down);
  window.addEventListener('pointermove', move); window.addEventListener('pointerup', up); window.addEventListener('pointercancel', up);
  return () => { list.removeEventListener('pointerdown', down); window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', up); };
}

// ------------------------------------------------------------------ drag to reorder, rows of any height (To-dos)
// Like dragReorder, but rows can wrap onto several lines: the drop place comes from the row centres
// measured when the drag starts, and the rows passed over slide by the dragged row's own height.
export function dragSort(list, { onDrop }) {
  let drag = null;
  const down = e => {
    const h = e.target.closest('.handle'); if (!h || h.closest('[data-chk-drag]') !== list) return;
    e.preventDefault(); e.stopPropagation();
    const row = h.closest('[data-id]'), all = [...list.children].filter(r => r.dataset.id);
    const mids = all.map(r => { const b = r.getBoundingClientRect(); return b.top + b.height / 2; });
    drag = { row, all, mids, from: all.indexOf(row), y0: e.clientY, dy: 0, h: row.offsetHeight };
    row.classList.add('is-dragging');
  };
  const target = () => {
    const mid = drag.mids[drag.from] + drag.dy;
    let to = drag.from;
    while (to < drag.all.length - 1 && mid > drag.mids[to + 1]) to++;
    while (to > 0 && mid < drag.mids[to - 1]) to--;
    return to;
  };
  const move = e => {
    if (!drag) return;
    drag.dy = e.clientY - drag.y0;
    const to = target();
    drag.all.forEach((r, i) => {
      if (r === drag.row) r.style.transform = `translateY(${drag.dy}px)`;
      else if (drag.from < to && i > drag.from && i <= to) r.style.transform = `translateY(${-drag.h}px)`;
      else if (drag.from > to && i >= to && i < drag.from) r.style.transform = `translateY(${drag.h}px)`;
      else r.style.transform = '';
    });
  };
  const up = () => {
    if (!drag) return;
    const to = target(), from = drag.from, ids = drag.all.map(r => r.dataset.id);
    drag.all.forEach(r => { r.style.transform = ''; }); drag.row.classList.remove('is-dragging');
    drag = null;
    if (to === from) return;
    const [moved] = ids.splice(from, 1); ids.splice(to, 0, moved);
    onDrop(ids);
  };
  list.addEventListener('pointerdown', down);
  window.addEventListener('pointermove', move); window.addEventListener('pointerup', up); window.addEventListener('pointercancel', up);
  return () => { list.removeEventListener('pointerdown', down); window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', up); };
}

// ------------------------------------------------------------------ double tap (splash dismiss)
export function onDoubleTap(el, fn, windowMs = 380) {
  let last = 0;
  const h = () => { const t = Date.now(); if (t - last < windowMs) { last = 0; fn(); } else last = t; };
  el.addEventListener('click', h);
  return () => el.removeEventListener('click', h);
}

// ------------------------------------------------------------------ auto-growing textarea (Ask box)
export function autoGrow(textarea, max = 180) {
  const fit = () => { textarea.style.height = 'auto'; textarea.style.height = Math.min(textarea.scrollHeight, max) + 'px'; };
  textarea.addEventListener('input', fit);
  return Object.assign(() => textarea.removeEventListener('input', fit), { fit, reset: () => (textarea.style.height = '') });
}
