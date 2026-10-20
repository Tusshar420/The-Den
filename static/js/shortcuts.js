// Keyboard shortcuts you can change in Settings > Shortcuts. Saved per device in localStorage
// (a phone has no keyboard, so they don't need to follow you around). A combo is a string like
// 'Mod+B' - Mod is ⌘ on a Mac and Ctrl elsewhere - or a single key like '/'.

const PREF = 'octopus-shortcuts';
export const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

// [id, what it does, default combo]
export const SHORTCUTS = [
  ['newTask', 'New task', 'Mod+/'],
  ['toggleSidebar', 'Collapse or expand the sidebar', 'Mod+B'],
  ['cycleSides', 'Right panel: Queue, then Messages, then Calendar, then close', 'Mod+Shift+B'],
  ['spotlight', 'Spotlight: search or ask Octopus anything', 'Alt+Space'],
  ['search', 'Search everything', '/'],
  ['approve', 'Approve the waiting request', 'A'],
  ['deny', 'Deny it', 'D'],
  ['briefing', 'Good morning briefing', 'G'],
  ['keys', 'Show the shortcuts list', '?'],
];
// Built in and not changeable - Settings refuses these so a custom shortcut can't hide them.
export const FIXED = [['Mod+K', 'New task'], ['N', 'New task'], ['[', 'Collapse or expand the sidebar'], ['1 – 6', 'Home, Projects, Recipes, Activity, …, Messages'], ['7', 'Calendar'], ['Escape', 'Close the panel']];
const RESERVED = new Set(['Mod+K', 'N', '[', 'Escape', '1', '2', '3', '4', '5', '6', '7']);

const DEFAULTS = Object.fromEntries(SHORTCUTS.map(([id, , combo]) => [id, combo]));
let custom = {};
try { custom = JSON.parse(localStorage.getItem(PREF) || '{}') || {}; } catch {}
const save = () => { try { localStorage.setItem(PREF, JSON.stringify(custom)); } catch {} };

export const bindingOf = id => (id in custom ? custom[id] : DEFAULTS[id]);   // '' = turned off
export const isDefault = id => bindingOf(id) === DEFAULTS[id];

// The combo a key press stands for, or '' for a lone modifier. Letters and digits come from the
// physical key so ⌥G stays 'Alt+G' rather than '©'; for other characters Shift is already baked in
// ('?' not 'Shift+/').
export function comboOf(e) {
  if (['Meta', 'Control', 'Alt', 'Shift', 'CapsLock', 'Dead'].includes(e.key)) return '';
  const m = /^(?:Key([A-Z])|Digit(\d))$/.exec(e.code || '');
  let key = m ? (m[1] || m[2]) : e.key.length === 1 ? e.key.toUpperCase() : e.key;
  if (key === ' ' || e.code === 'Space') key = 'Space';   // ⌥Space types a non-breaking space on a Mac
  if (key === '+') key = 'Plus';      // '+' separates the parts
  const shift = e.shiftKey && (m || key.length > 1);
  return [(e.metaKey || e.ctrlKey) && 'Mod', e.altKey && 'Alt', shift && 'Shift', key].filter(Boolean).join('+');
}

// Which changeable shortcut this key press triggers. While typing in a box only combos with ⌘/Ctrl
// count, so plain letters still type.
export function shortcutFor(e, typing) {
  const combo = comboOf(e);
  if (!combo || (typing && !combo.startsWith('Mod+') && !/.\+Space$/.test(combo))) return null;
  const hit = SHORTCUTS.find(([id]) => bindingOf(id) === combo);
  return hit ? hit[0] : null;
}

// Set id to combo. Returns an error message, or '' on success. Taking a combo another shortcut uses
// moves it here (the other one is left without a shortcut) and says so.
export function setBinding(id, combo) {
  if (RESERVED.has(combo)) return `${label(combo)} is built in - pick another`;
  const other = SHORTCUTS.find(([o]) => o !== id && bindingOf(o) === combo);
  if (other) custom[other[0]] = '';
  if (combo === DEFAULTS[id]) delete custom[id]; else custom[id] = combo;
  save();
  return other ? `${label(combo)} moved from "${other[1]}", which now has no shortcut` : '';
}
export function resetBinding(id) {
  const other = SHORTCUTS.find(([o]) => o !== id && bindingOf(o) === DEFAULTS[id]);
  if (other) custom[other[0]] = '';     // someone else had taken our default - same rule as setBinding
  delete custom[id];
  save();
}
export function resetAll() { custom = {}; save(); }

// 'Mod+Shift+B' -> '⌘ ⇧ B' on a Mac, 'Ctrl + Shift + B' elsewhere.
const MAC_NAMES = { Mod: '⌘', Alt: '⌥', Shift: '⇧' };
const NAMES = { Escape: 'Esc', ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→', Enter: '↵', Plus: '+' };
export function label(combo) {
  if (!combo) return '—';
  const parts = combo.split('+').map((p, i, a) => i < a.length - 1 ? (IS_MAC ? MAC_NAMES[p] : p === 'Mod' ? 'Ctrl' : p) : NAMES[p] || p);
  return parts.join(IS_MAC ? ' ' : ' + ');
}
