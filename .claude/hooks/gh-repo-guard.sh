#!/usr/bin/env bash
# Blocks repo-scoped `gh` calls that omit --repo.
#
# This clone is a fork of future-research/candidate-assessment, so bare `gh`
# resolves to the FORK PARENT, not origin. An unscoped `gh issue list` reads the
# assessment org's issues, and `gh issue create` would file there. That has
# already happened once (14 stray issues, 2026-08-06). CLAUDE.md forbids
# touching upstream; this hook enforces it.
set -uo pipefail

REQUIRED_REPO="Josie29/knowledge-graph-coach-dashboard"

# Subcommands that resolve a target repository. `gh auth`, `gh config`, and
# `gh --version` are intentionally absent: they are not repo-scoped.
REPO_SCOPED='issue|pr|release|label|workflow|run|api|browse|secret|variable|ruleset|repo'

deny() {
  jq -cn --arg reason "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 0
}

cmd=$(jq -r '.tool_input.command // empty' 2>/dev/null)
[ -z "$cmd" ] && exit 0

# Split on chaining operators so `foo && gh issue list` is inspected too.
while IFS= read -r segment; do
  # Strip leading whitespace and any env-var prefixes (FOO=bar gh ...).
  seg=$(printf '%s' "$segment" | sed -E 's/^[[:space:]]*//; s/^([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*//')

  # First token must be gh (bare or an absolute path ending in /gh).
  read -r bin sub _rest <<<"$seg"
  case "$bin" in
    gh | */gh) ;;
    *) continue ;;
  esac

  printf '%s' "$sub" | grep -qE "^($REPO_SCOPED)$" || continue

  # --repo <owner/name>, --repo=<owner/name>, or -R <owner/name>
  if printf '%s' "$seg" | grep -qE '(--repo[= ]|[[:space:]]-R[= ])'; then
    continue
  fi

  deny "\`gh $sub\` without --repo resolves to the FORK PARENT (future-research/candidate-assessment), not origin. CLAUDE.md forbids touching upstream. Re-run with: gh $sub ... --repo $REQUIRED_REPO"
done <<<"$(printf '%s' "$cmd" | tr ';|&' '\n')"

exit 0
