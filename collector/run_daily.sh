#!/bin/sh
# Daily refresh: collect -> privacy check -> commit docs/data -> push.
# Configure with environment variables (see README), e.g. from a systemd EnvironmentFile:
#   LIMIT_TRACKER_TRANSCRIPTS, LIMIT_TRACKER_STREAMS, LIMIT_TRACKER_SAMPLES, LIMIT_TRACKER_PLAN
#   LIMIT_TRACKER_PUSH=0 to commit without pushing.
set -eu
cd "$(dirname "$0")/.."
python3 collector/collect.py
python3 tools/privacy_check.py
git add docs/data
if git diff --cached --quiet; then
  echo "no data changes"
  exit 0
fi
git commit -q -m "data: update $(date -u +%Y-%m-%d)"
if [ "${LIMIT_TRACKER_PUSH:-1}" != "0" ]; then
  git push -q
  echo "committed and pushed"
else
  echo "committed (push disabled)"
fi
