// Inline SVG icons used by the design. All 20×20 viewBox, stroke = currentColor, 1.6–1.8 stroke width.
// Usage: button.innerHTML = icon('search', 16);

const P = {
  home:     '<path d="M3 9.2 10 3.8l7 5.4V16a1 1 0 0 1-1 1h-3.5v-4.5h-5V17H4a1 1 0 0 1-1-1z"/>',
  projects: '<path d="M2.5 6.3A1.5 1.5 0 0 1 4 4.8h3.6l1.7 1.9H16a1.5 1.5 0 0 1 1.5 1.5v6.8A1.5 1.5 0 0 1 16 16.5H4A1.5 1.5 0 0 1 2.5 15z"/>',
  recipes:  '<path d="M5.5 3h9a1 1 0 0 1 1 1v13l-5.5-3.3L4.5 17V4a1 1 0 0 1 1-1z"/><path d="M8 7.5h4"/>',
  activity: '<path d="M2.5 10.5h3.2l2.1-5.5 4.2 10 2.1-4.5h3.4"/>',
  sort:     '<path d="M6.3 3.8v12.4M3.1 13.1l3.2 3.1 3.1-3.1M13.8 16.2V3.8M10.6 6.9l3.2-3.1 3.1 3.1"/>',
  filter:   '<path d="M3.1 5h13.8M5.6 10h8.8M8.1 15h3.8"/>',
  search:   '<circle cx="8.8" cy="8.8" r="5.6"/><path d="M13.1 13.1 17.5 17.5"/>',
  chevron:  '<path d="M4.3 7.1l5.7 5.7 5.7-5.7"/>',
  image:    '<rect x="2.5" y="3.5" width="15" height="13" rx="3"/><circle cx="7.5" cy="8" r="1.5"/><path d="m3 15 4.5-4.5 3 3 2-2L17 16"/>',
  polish:   '<path d="M9 2.8l1.5 4.2 4.2 1.5-4.2 1.5L9 14.2 7.5 10 3.3 8.5 7.5 7z"/><path d="M15 12.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>',
  density:  '<path d="M3 3.5h14M3 10h14M3 16.5h14"/>',
  densityCompact: '<path d="M3 6h14M3 10h14M3 14h14"/>',
  // notifications (see notifications.js › KINDS)
  bell:    '<path d="M5 8.5a5 5 0 0 1 10 0v3l1.5 2.5h-13L5 11.5zM8.3 16.5a2 2 0 0 0 3.4 0"/>',
  close:   '<path d="M5.5 5.5l9 9M14.5 5.5l-9 9"/>',
  nCheck:  '<path d="M4.5 10.5l3.5 3.5 7.5-8"/>',
  nBubble: '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h9A1.5 1.5 0 0 1 16 5.5v6a1.5 1.5 0 0 1-1.5 1.5H9l-3.5 3v-3A1.5 1.5 0 0 1 4 11.5z"/>',
  nClock:  '<path d="M10 3.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zM10 6.5V10l2.5 1.5"/>',
  nQueue:  '<path d="M3.5 5.5h8M3.5 10h5M3.5 14.5h5M14.5 16V8M11.5 11l3-3 3 3"/>',
  nAlert:  '<path d="M10 3.5 17 16H3zM10 8.5V12M10 14.2v.1"/>',
  nEye:    '<path d="M2.5 10S5 5 10 5s7.5 5 7.5 5-2.5 5-7.5 5-7.5-5-7.5-5zM10 8a2 2 0 1 0 0 4 2 2 0 0 0 0-4z"/>',
  nMoon:   '<path d="M15.5 12.5A6 6 0 0 1 7.5 4.5a6 6 0 1 0 8 8z"/>',
  nSpark:  '<path d="M10 3l1.6 4.4L16 9l-4.4 1.6L10 15l-1.6-4.4L4 9l4.4-1.6z"/>',
  nPlus:   '<path d="M10 4.5v11M4.5 10h11"/>',
  nInfo:   '<path d="M10 9v5M10 6v.1"/>',
};

export function icon(name, size = 20, { strokeWidth = 1.7 } = {}) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="${strokeWidth}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ''}</svg>`;
}
