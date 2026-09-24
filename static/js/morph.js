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
//
// Only nodes and attributes morph itself created are ever touched. Browser extensions
// (Grammarly, password managers...) inject their own elements next to text fields and
// add attributes to them; treating those as stale would rebuild - and so blur - the
// field the user is typing in, on every keystroke.

const ours = new WeakSet();          // elements morph inserted
const ownAttrs = new WeakMap();      // element -> attribute names morph set on it

const isOurs = n => n.nodeType !== 1 || ours.has(n);
const nextOurs = n => { while (n && !isOurs(n)) n = n.nextSibling; return n; };
const keyOf = n => (n.nodeType === 1 ? n.getAttribute('data-key') : null);
const sameKind = (a, b) => a.nodeType === b.nodeType && (a.nodeType !== 1 || a.tagName === b.tagName);

function adopt(el) {
  for (const e of [el, ...el.querySelectorAll('*')]) {
    ours.add(e);
    ownAttrs.set(e, new Set(e.getAttributeNames()));
  }
}

function syncAttrs(a, b) {
  const liveStyle = a.hasAttribute('data-live-style');
  const want = b.getAttributeNames();
  for (const name of ownAttrs.get(a) || []) {
    if (liveStyle && name === 'style') continue;
    if (!b.hasAttribute(name)) a.removeAttribute(name);
  }
  for (const name of want) {
    if (liveStyle && name === 'style') continue;
    const value = b.getAttribute(name);
    if (a.getAttribute(name) !== value) a.setAttribute(name, value);
  }
  ownAttrs.set(a, new Set(want));
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
  for (const n of parent.childNodes) { const k = keyOf(n); if (k != null && isOurs(n)) keyed.set(k, n); }
  let cur = nextOurs(parent.firstChild);
  for (const nb of want) {
    const k = keyOf(nb);
    let match = null;
    if (k != null) {
      const old = keyed.get(k);
      if (old && sameKind(old, nb)) { match = old; keyed.delete(k); }
    } else if (cur && keyOf(cur) == null && sameKind(cur, nb)) {
      match = cur;
    }
    if (match) {
      if (match === cur) cur = nextOurs(cur.nextSibling);
      else parent.insertBefore(match, cur);
      patch(match, nb, added);
    } else {
      parent.insertBefore(nb, cur);
      if (nb.nodeType === 1) { adopt(nb); added.push(nb); }
    }
  }
  // Everything of ours from here on is stale; foreign nodes stay where they are.
  while (cur) { const n = nextOurs(cur.nextSibling); parent.removeChild(cur); cur = n; }
}

const tpl = document.createElement('template');

// Morph root's children to match html. Returns the elements that were newly inserted.
export function morph(root, html) {
  tpl.innerHTML = html;
  const added = [];
  morphChildren(root, tpl.content, added);
  return added;
}
