#!/usr/bin/env bash
# Fail when staged text contains obvious literal API keys / tokens (not env placeholders).
#
# Install as pre-commit hook (repo root):
#   chmod +x scripts/check_staged_no_plain_credentials.sh
#   ln -sf ../../scripts/check_staged_no_plain_credentials.sh .git/hooks/pre-commit
set -euo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root"

# Only scan plausible text paths from the index (avoid huge/binary noise).
text_ext_re='\.(yaml|yml|env(\..*)?|toml|json|md|txt|sh|py)$'

found=0
while IFS= read -r f; do
  [[ -z "${f:-}" ]] && continue
  if [[ ! "$f" =~ $text_ext_re ]]; then
    continue
  fi
  blob=$(git show ":$f" 2>/dev/null || true)
  [[ -z "$blob" ]] && continue

  if printf '%s\n' "$blob" | grep -qE \
    '\b(ark-[a-zA-Z0-9_-]{12,}|sk-[a-zA-Z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|xox[baprs]-[0-9A-Za-z-]{10,})\b'; then
    echo "check_staged_no_plain_credentials: suspected literal token in staged file: $f" >&2
    printf '%s\n' "$blob" | grep -nE \
      '\b(ark-[a-zA-Z0-9_-]{12,}|sk-[a-zA-Z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|xox[baprs]-[0-9A-Za-z-]{10,})\b' \
      >&2 || true
    found=1
  fi
done < <(git diff --cached --name-only --diff-filter=ACMRT)

if [[ "$found" -ne 0 ]]; then
  echo "" >&2
  echo "Remove plaintext secrets from staged files; use \"\${ENV_VAR}\" placeholders (see configs/base.yaml)." >&2
  echo "Keep real keys in .env (gitignored)." >&2
  exit 1
fi

exit 0
