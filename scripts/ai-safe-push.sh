#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 0 ]]; then
  echo "usage: ai-safe-push.sh" >&2
  exit 2
fi

: "${HEAD_REF:?HEAD_REF is required}"
: "${EVENT_HEAD_SHA:?EVENT_HEAD_SHA is required}"

current_branch=$(git symbolic-ref --quiet --short HEAD) || {
  echo "refusing publication from detached HEAD" >&2
  exit 1
}

if [[ "$current_branch" != "$HEAD_REF" ]]; then
  echo "refusing publication: current branch '$current_branch' != HEAD_REF '$HEAD_REF'" >&2
  exit 1
fi

if [[ "$HEAD_REF" == "main" ]]; then
  echo "refusing publication: automated fixes may not publish to main" >&2
  exit 1
fi

if [[ "$(git rev-parse HEAD)" != "$EVENT_HEAD_SHA" ]]; then
  echo "refusing publication: checkout no longer matches reviewed head" >&2
  exit 1
fi

if ! git diff --cached --quiet; then
  echo "refusing publication: index already contains staged changes" >&2
  exit 1
fi

remote_head=$(git ls-remote --heads origin "refs/heads/$HEAD_REF" | awk '{print $1}')
if [[ "$remote_head" != "$EVENT_HEAD_SHA" ]]; then
  echo "refusing publication: remote branch advanced since review" >&2
  exit 1
fi

is_ignored_restoration() {
  case "$1" in
    CLAUDE.md|CLAUDE.local.md|.mcp.json|.claude.json|.gitmodules|.ripgreprc|.ai/current-review.json|.ai/current-task.md)
      return 0
      ;;
    .claude|.claude/*|.claude-pr|.claude-pr/*|.husky|.husky/*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_forbidden_change() {
  case "$1" in
    .env.example)
      return 1
      ;;
    .github/workflows/*|.env|.env.*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

declare -a publishable=()

# Deduplication by scanning the collected list rather than with an associative
# array. `declare -A` is bash 4, and macOS still ships bash 3.2 as /bin/bash,
# so this script aborted at startup on a stock Mac — before any of its refusals
# could run, which is the worst moment for a safety script to fail. The lists
# here are a handful of paths, so the scan costs nothing, and comparing whole
# strings also avoids the separator ambiguity a newline-delimited "seen" string
# would have for a path that itself contains a newline.
already_collected() {
  local needle="$1" candidate
  for candidate in ${publishable+"${publishable[@]}"}; do
    if [[ "$candidate" == "$needle" ]]; then
      return 0
    fi
  done
  return 1
}

consider_path() {
  local path="$1"

  if is_forbidden_change "$path"; then
    echo "refusing publication: forbidden automated change '$path'" >&2
    exit 1
  fi

  if is_ignored_restoration "$path"; then
    return 0
  fi

  if ! already_collected "$path"; then
    publishable+=("$path")
  fi
}

while IFS= read -r -d '' path; do
  consider_path "$path"
done < <(git diff --name-only -z --no-renames)

while IFS= read -r -d '' path; do
  consider_path "$path"
done < <(git ls-files --others --exclude-standard -z)

if [[ ${#publishable[@]} -eq 0 ]]; then
  echo "refusing publication: no publishable fix changes" >&2
  exit 1
fi

git add -- "${publishable[@]}"

if git diff --cached --quiet; then
  echo "refusing publication: selected fix produced no staged diff" >&2
  exit 1
fi

git commit -m "fix: address Codex review findings"

if [[ "$(git rev-parse HEAD^)" != "$EVENT_HEAD_SHA" ]]; then
  echo "refusing publication: fix commit is not a direct child of reviewed head" >&2
  exit 1
fi

remote_head=$(git ls-remote --heads origin "refs/heads/$HEAD_REF" | awk '{print $1}')
if [[ "$remote_head" != "$EVENT_HEAD_SHA" ]]; then
  echo "refusing publication: remote branch advanced before push" >&2
  exit 1
fi

git push origin "HEAD:refs/heads/$HEAD_REF"
