// Octopus character. Pure function → SVG markup string, no dependencies.
// Usage: el.innerHTML = octopusSVG('work');  (size comes from the container / .octo--* class)
//
// Moods and when to use them:
//   'wait'  approval pending        (big eyes, highlight glint, amber glow, gentle bob)
//   'work'  a task is running       (pupils look down, teal glow, fast tentacles)
//   'sleep' rate-limited / onboarding (closed eyes, slow tentacles, floating "z")
//   'idle'  nothing to do / avatars (happy arched eyes)
//   'error' last task failed        (small pupils, angry brows)
//   'dance' loading spinner         (happy eyes, blush, hop + sway, ground shadow)

const CORAL = '#FF7B5C';
const INK = '#0B1016';

export function octopusSVG(mood = 'idle', { title = 'Octopus' } = {}) {
  const slow = mood === 'sleep', dance = mood === 'dance', fast = mood === 'work';
  const speed = slow ? 5 : dance ? 0.45 : fast ? 1.1 : 2.2;

  const tentacles = [18, 27, 36, 45, 54, 62].map((x, i) => {
    const odd = i % 2;
    const d = `M${x} 48 q ${odd ? 5 : -5} 10 ${odd ? -1 : 1} 18 q ${odd ? -3 : 3} 5 ${odd ? 4 : -4} 6`;
    return `<g style="transform-origin:${x}px 50px;animation:octo-wig ${speed}s ease-in-out ${(i * 0.16).toFixed(2)}s infinite">`
      + `<path d="${d}" stroke="${CORAL}" stroke-width="6.5" stroke-linecap="round" fill="none"/></g>`;
  }).join('');

  let eyes;
  if (mood === 'sleep') {
    eyes = `<path d="M26 34 q5 4 10 0 M44 34 q5 4 10 0" stroke="${INK}" stroke-width="2.6" stroke-linecap="round" fill="none"/>`;
  } else if (mood === 'idle' || dance) {
    eyes = `<path d="M26 35 q5 -6 10 0 M44 35 q5 -6 10 0" stroke="${INK}" stroke-width="2.8" stroke-linecap="round" fill="none"/>`;
  } else {
    const py = mood === 'work' ? 3 : mood === 'wait' ? -1.5 : 0;
    const r = mood === 'wait' ? 7.5 : 6.5;
    const pr = mood === 'error' ? 2.2 : 3.4;
    eyes = `<g style="transform-origin:40px 34px;animation:octo-blink 4.5s infinite">`
      + `<ellipse cx="31" cy="34" rx="${r - 1}" ry="${r}" fill="#FFF3EC"/><ellipse cx="49" cy="34" rx="${r - 1}" ry="${r}" fill="#FFF3EC"/>`
      + `<circle cx="31" cy="${34 + py}" r="${pr}" fill="${INK}"/><circle cx="49" cy="${34 + py}" r="${pr}" fill="${INK}"/>`
      + (mood === 'wait' ? `<circle cx="32.2" cy="31.4" r="1.1" fill="#fff"/><circle cx="50.2" cy="31.4" r="1.1" fill="#fff"/>` : '')
      + (mood === 'error' ? `<path d="M25 25 l9 3 M55 25 l-9 3" stroke="${INK}" stroke-width="2.4" stroke-linecap="round"/>` : '')
      + `</g>`;
  }

  const glow = mood === 'wait' ? `<circle cx="40" cy="34" r="34" fill="rgba(255,181,71,.12)"/>`
    : mood === 'work' ? `<circle cx="40" cy="34" r="34" fill="rgba(69,224,200,.10)"/>` : '';
  const shadow = dance ? `<ellipse cx="40" cy="79" rx="17" ry="3" fill="rgba(255,123,92,.32)" style="transform-origin:40px 79px;animation:octo-shadow .9s ease-in-out infinite"/>` : '';
  const blush = dance ? `<ellipse cx="23" cy="42" rx="4" ry="2.4" fill="#FF5470" opacity=".45"/><ellipse cx="57" cy="42" rx="4" ry="2.4" fill="#FF5470" opacity=".45"/>` : '';
  const zz = slow ? `<text x="62" y="10" fill="#A58BFF" font-size="12" font-family="JetBrains Mono" style="animation:octo-zz 2.4s infinite">z</text>` : '';
  const bodyStyle = dance ? ` style="transform-origin:40px 72px;animation:octo-dance .9s ease-in-out infinite"` : '';
  const svgStyle = mood === 'wait' ? 'overflow:visible;animation:octo-bob 2s ease-in-out infinite' : 'overflow:visible';

  return `<svg class="octo" viewBox="0 0 80 80" width="100%" height="100%" role="img" aria-label="${title}" style="${svgStyle}">`
    + glow + shadow
    + `<g${bodyStyle}><g>${tentacles}</g>`
    + `<path d="M13 40 C13 16 26 6 40 6 C54 6 67 16 67 40 C67 49 57 53 40 53 C23 53 13 49 13 40 Z" fill="${CORAL}"/>`
    + `<ellipse cx="29" cy="17" rx="7" ry="4" fill="#fff" opacity=".2" transform="rotate(-20 29 17)"/>`
    + eyes + blush + `</g>` + zz + `</svg>`;
}

// Which mood the global Octopus (hero, sidebar, favicon) should show for a queue state.
export function moodFor({ empty, approval, rateLimited, running, allHeld, hasErrors }) {
  if (empty) return 'sleep';
  if (approval) return 'wait';
  if (rateLimited) return 'sleep';
  if (running) return 'work';
  if (allHeld) return 'idle';
  return hasErrors ? 'error' : 'idle';
}
