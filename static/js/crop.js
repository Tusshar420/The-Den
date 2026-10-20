// A small picture editor shown when a project picture or client logo is chosen: drag to move, slide
// (or scroll) to zoom, then Save. The result is the square PNG that gets uploaded.
// It lives on document.body, outside the app's re-rendered tree, so a redraw never interrupts a drag.
// Zoom all the way out and the whole picture fits inside the square (transparent around it).

const VIEW = 280, OUT = 256, MAX_ZOOM = 4;

// Resolves to a PNG Blob, null if cancelled, or the file itself when the browser can't draw it.
export async function cropImage(file, title = 'Adjust picture') {
  let bmp;
  try { bmp = await createImageBitmap(file); } catch { return file; }
  const { width: w, height: h } = bmp;
  const cover = VIEW / Math.min(w, h), minZ = Math.min(w, h) / Math.max(w, h);
  let z = 1, ox = 0, oy = 0;                     // zoom (1 = fills the square) and the picture's offset from centre

  const root = document.createElement('div');
  root.className = 'crop';
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-label', title);
  root.innerHTML = `<div class="crop__box"><div class="crop__title">${title}</div>
    <div class="crop__view" style="width:${VIEW}px;height:${VIEW}px"><canvas width="${VIEW}" height="${VIEW}"></canvas></div>
    <label class="crop__zoom"><span class="meta">Size</span><input type="range" min="${minZ}" max="${MAX_ZOOM}" step="0.01" value="1" aria-label="Zoom"></label>
    <div class="crop__acts"><button class="btn btn--ghost btn--sm" data-k="fit">Fit whole picture</button><button class="btn btn--ghost btn--sm" data-k="reset">Reset</button>
      <span class="grow"></span><button class="btn btn--ghost btn--sm" data-k="cancel">Cancel</button><button class="btn btn--primary btn--sm" data-k="save">Save</button></div></div>`;
  const cv = root.querySelector('canvas'), range = root.querySelector('input');

  const clamp = () => {
    const s = cover * z;
    for (const [dim, key] of [[w, 'x'], [h, 'y']]) {
      const lim = Math.abs(dim * s - VIEW) / 2;
      if (key === 'x') ox = Math.max(-lim, Math.min(lim, ox)); else oy = Math.max(-lim, Math.min(lim, oy));
    }
  };
  const paint = (ctx, size) => {
    const k = size / VIEW, s = cover * z * k;
    ctx.clearRect(0, 0, size, size);
    ctx.drawImage(bmp, size / 2 + ox * k - (w * s) / 2, size / 2 + oy * k - (h * s) / 2, w * s, h * s);
  };
  const draw = () => {
    clamp();
    range.value = z;
    const ctx = cv.getContext('2d');
    paint(ctx, VIEW);
  };

  return new Promise(resolve => {
    const done = v => { root.remove(); document.removeEventListener('keydown', onKey, true); bmp.close && bmp.close(); resolve(v); };
    const onKey = e => { if (e.key === 'Escape') { e.stopPropagation(); done(null); } };
    document.addEventListener('keydown', onKey, true);

    let drag = null;
    cv.addEventListener('pointerdown', e => { cv.setPointerCapture(e.pointerId); drag = { x: e.clientX - ox, y: e.clientY - oy }; });
    cv.addEventListener('pointermove', e => { if (drag) { ox = e.clientX - drag.x; oy = e.clientY - drag.y; draw(); } });
    const end = () => { drag = null; };
    cv.addEventListener('pointerup', end);
    cv.addEventListener('pointercancel', end);
    cv.addEventListener('wheel', e => { e.preventDefault(); z = Math.max(minZ, Math.min(MAX_ZOOM, z * (e.deltaY < 0 ? 1.06 : 1 / 1.06))); draw(); }, { passive: false });
    range.addEventListener('input', () => { z = Number(range.value); draw(); });
    root.addEventListener('click', e => {
      const k = e.target.closest('[data-k]');
      if (e.target === root) return done(null);
      if (!k) return;
      if (k.dataset.k === 'cancel') return done(null);
      if (k.dataset.k === 'fit') { z = minZ; ox = oy = 0; return draw(); }
      if (k.dataset.k === 'reset') { z = 1; ox = oy = 0; return draw(); }
      const out = Object.assign(document.createElement('canvas'), { width: OUT, height: OUT });
      paint(out.getContext('2d'), OUT);
      out.toBlob(b => done(b || file), 'image/png');
    });

    document.body.appendChild(root);
    draw();
    root.querySelector('[data-k="save"]').focus();
  });
}
