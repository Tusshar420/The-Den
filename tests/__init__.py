"""Runs before any test module is imported - including ones that import worker or api
before tests.helpers. worker reads ACTIVITYSMITH_API_KEY at import, so without this a
test run from a shell that has the real keys sends real phone pushes (and model calls)."""

import os

for _var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ACTIVITYSMITH_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
    os.environ.pop(_var, None)
