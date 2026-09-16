#!/usr/bin/env bash
# Commit and push whatever the generator just changed.
#
# Called twice: by the generator itself as soon as a pass captures something
# (so an episode is published the minute it is found, not when the whole
# watch ends), and once more after the run. The second call finds nothing to
# do when the first already published.
set -euo pipefail

branch="${PUBLISH_BRANCH:?PUBLISH_BRANCH is required}"

git add feed_*.xml README.md
if git diff --cached --quiet; then
  echo "Nothing to publish."
  exit 0
fi

if [ -n "${MOHLIO_CHANGED:-}" ]; then
  git commit -m "feeds: update ${MOHLIO_CHANGED}"
else
  git commit -m "feeds: update"
fi

for attempt in 1 2 3 4 5; do
  if git pull --rebase --autostash origin "$branch" && git push origin "HEAD:$branch"; then
    echo "Published on attempt $attempt."
    exit 0
  fi
  git rebase --abort 2>/dev/null || true
  if [ "$attempt" -eq 5 ]; then
    echo "::error::Failed to push after 5 attempts."
    exit 1
  fi
  sleep $(( (RANDOM % 10) + attempt * 5 ))
done
