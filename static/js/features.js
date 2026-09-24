// UI the design includes but the backend doesn't support yet. Flip one on only once
// its endpoint exists - never ship it pointing at nothing.
export const FEATURES = {
  // Attach-image buttons (Ask box, New task sheet, chat composer). Needs an upload
  // endpoint and passing images through to Claude Code and the API models.
  images: false,
};
