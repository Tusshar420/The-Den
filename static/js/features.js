// UI the design includes but the backend doesn't support yet. Flip one on only once
// its endpoint exists - never ship it pointing at nothing.
export const FEATURES = {
  // Attach-image buttons (Ask box, New task sheet, chat composer): POST /uploads, then the
  // ids go with the prompt (see attach.js and uploads.py).
  images: true,
};
