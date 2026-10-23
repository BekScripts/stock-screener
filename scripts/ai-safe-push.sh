#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 0 ]]; then
  echo "usage: ai-safe-push.sh" >&2
  exit 2
fi

: "${HEAD_REF:?HEAD_REF is required}"
: "${EVENT_HEAD_SHA:?EVENT_HEAD_SHA is required}"

current_branch=$(git symbolic-ref --quiet --short HEAD) || {
  echo "refusing push from detached HEAD" >&2
  exit 1
}

if [[ "$current_branch" != "$HEAD_REF" ]]; then
  echo "refusing push: current branch '$current_branch' != HEAD_REF '$HEAD_REF'" >&2
  exit 1
fi

current_head=$(git rev-parse HEAD)
if [[ "$current_head" == "$EVENT_HEAD_SHA" ]]; then
  echo "refusing push: no fix commit exists above reviewed head" >&2
  exit 1
fi

parent=$(git rev-parse HEAD^)
if [[ "$parent" != "$EVENT_HEAD_SHA" ]]; then
  echo "refusing push: fix commit is not a direct child of reviewed head" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "refusing push: working tree is not clean" >&2
  exit 1
fi

remote_head=$(git ls-remote --heads origin "refs/heads/$HEAD_REF" | awk '{print $1}')
if [[ "$remote_head" != "$EVENT_HEAD_SHA" ]]; then
  echo "refusing push: remote branch advanced since review" >&2
  exit 1
fi

# Deliberately non-force. If the branch advances after the check above, Git
# rejects this update as non-fast-forward because HEAD is a child of the
# reviewed EVENT_HEAD_SHA, not the newer remote head.
git push origin "HEAD:refs/heads/$HEAD_REF"
