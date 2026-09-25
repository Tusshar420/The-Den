// "Polish text" button behaviour (Ask box, New-task sheet, chat reply box).
// Production: call a backend endpoint that asks a cheap model to rewrite the text.
// Fallback: polishLocal() — deterministic cleanup, no network.

export const POLISH_SYSTEM_PROMPT =
  "You polish requests a developer sends to an AI assistant (Claude Code or a chat model). Rewrite the text so it is clear, " +
  "specific and well-formed: fix grammar, spelling and punctuation, and tighten vague wording. Keep the same intent, language " +
  "and tone. Keep file names, commands, code, numbers and names exactly as written. Do not add new requirements or explanations. " +
  "Return only the rewritten text, with no quotes or preamble.";

const SHORTHAND = {
  pls: 'please', plz: 'please', u: 'you', ur: 'your', 'w/': 'with', 'w/o': 'without', thx: 'thanks', btw: 'by the way',
  asap: 'as soon as possible', i: 'I', im: "I'm", dont: "don't", cant: "can't", wont: "won't", doesnt: "doesn't", isnt: "isn't",
  idk: "I don't know", rn: 'right now', abt: 'about', smth: 'something', b4: 'before', msg: 'message',
};

export function polishLocal(text) {
  let s = text.replace(/\s+/g, ' ').trim()
    .split(' ').map(w => { const m = w.match(/^(.*?)([.,!?:;]*)$/); return (SHORTHAND[m[1].toLowerCase()] || m[1]) + m[2]; }).join(' ');
  s = s.charAt(0).toUpperCase() + s.slice(1);
  if (!/[.?!]$/.test(s)) s += /^(what|why|how|when|where|who|which|can|could|should|would|is|are|do|does|did|will)\b/i.test(s) ? '?' : '.';
  return s;
}

// requestPolish: async (text) => string   e.g. text => api('/octopus/polish', { method: 'POST', body: { text } }).then(r => r.text)
export async function polish(text, requestPolish) {
  const original = text;
  if (!original.trim()) return { text: original, changed: false };
  let out = '';
  try { out = requestPolish ? await requestPolish(original) : ''; } catch { out = ''; }
  out = String(out || '').trim().replace(/^["\u201c]+|["\u201d]+$/g, '').trim() || polishLocal(original);
  return { text: out, changed: out !== original.trim(), original };
}

// Wire a polish button to a textarea. UI contract:
//  - disabled while the field is empty (opacity .4)
//  - while running: icon → 15px spinner, button disabled
//  - on change: replace text in place, toast "Text polished" with Undo (restores original)
//  - no change: toast "Already reads well"
export function bindPolish(button, textarea, { requestPolish, toast, onChange }) {
  const sync = () => { button.disabled = !textarea.value.trim() || button.classList.contains('is-busy'); };
  textarea.addEventListener('input', sync); sync();
  button.addEventListener('click', async () => {
    button.classList.add('is-busy'); sync();
    const r = await polish(textarea.value, requestPolish);
    button.classList.remove('is-busy');
    if (r.changed) {
      textarea.value = r.text; onChange?.(r.text);
      toast('Text polished', { action: 'Undo', onAction: () => { textarea.value = r.original; onChange?.(r.original); sync(); } });
    } else toast('Already reads well');
    sync();
  });
}
