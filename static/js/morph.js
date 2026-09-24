// Patch a live DOM tree to match freshly rendered HTML, instead of replacing it.
//
// Views render template strings; swapping innerHTML every poll (5 s) and every
// countdown tick (1 s) would close the phone keyboard mid-sentence, replay entrance
// animations and reset scroll positions. morph() keeps every element that still
// exists and only touches what changed:
//   - children are matched by data-key when present (reordered lists move, not rebuild),
//     otherwise by position + tag;
//   - an input/textarea that has focus keeps its value (the user is typing in it);
//   - elements marked data-live-style keep their inline style (gestures animate it);
//   - newly inserted elements are returned, so the caller can bind gestures to them.

const keyOf = n => (n.nodeType === 1 ? n.getAttribute('data-key') : null);
const sameKind = (a, b) => a.nodeType === b.nodeType && (a.nodeType !== 1 || a.tagName === b.tagName);

function syncAttrs(a, b) {
  const liveStyle = a.hasAttribute('data-live-style');
  for (const { name } of [...a.attributes]) {
    if (liveStyle && name === 'style') continue;
    if (!b.hasAttribute(name)) a.removeAttribute(name);
  }
  for (const { name, value } of [...b.attributes]) {
    if (liveStyle && name === 'style') continue;
    if (a.getAttribute(name) !== value) a.setAttribute(name, value);
  }
}

function patch(a, b, added) {
  if (a.nodeType !== 1) {
    if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue;
    return;
  }
  syncAttrs(a, b);
  if (a.tagName === 'TEXTAREA' || a.tagName === 'INPUT') {
    if (a !== document.activeElement && a.value !== b.value) a.value = b.value;
    if (a.tagName === 'TEXTAREA') return;   // its children are its value
  }
  morphChildren(a, b, added);
}

function morphChildren(parent, next, added) {
  const want = [...next.childNodes];
  const keyed = new Map();
  for (const n of parent.childNodes) { const k = keyOf(n); if (k != null) keyed.set(k, n); }
  want.forEach((nb, i) => {
    const cur = parent.childNodes[i] || null;
    const k = keyOf(nb);
    let match = null;
    if (k != null) {
      const old = keyed.get(k);
      if (old && sameKind(old, nb)) { match = old; keyed.delete(k); }
    } else if (cur && keyOf(cur) == null && sameKind(cur, nb)) {
      match = cur;
    }
    if (match) {
      if (match !== cur) parent.insertBefore(match, cur);
      patch(match, nb, added);
    } else {
      parent.insertBefore(nb, cur);
      if (nb.nodeType === 1) added.push(nb);
    }
  });
  while (parent.childNodes.length > want.length) parent.removeChild(parent.lastChild);
}

const tpl = document.createElement('template');

// Morph root's children to match html. Returns the elements that were newly inserted.
export function morph(root, html) {
  tpl.innerHTML = html;
  const added = [];
  morphChildren(root, tpl.content, added);
  return added;
}
