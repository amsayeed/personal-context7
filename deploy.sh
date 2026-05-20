#!/usr/bin/env bash
# Personal Context7 → Railway, one-shot deploy.
#
# Usage:
#   ./deploy.sh                  # interactive: prompts for KB git remote + API key
#   ./deploy.sh --non-interactive
#
# Env you can preset to skip prompts:
#   PKB_API_KEY            (bearer token clients will send)
#   PKB_KB_GIT_REMOTE      (https://x:GITHUB_TOKEN@github.com/you/notes.git)
#   PKB_KB_GIT_BRANCH      (default: main)
#   PKB_RAILWAY_PROJECT    (existing project name; otherwise we create one)
#   PKB_RAILWAY_SERVICE    (service name; default: pkb)
#   PKB_RAILWAY_REGION     (default: us-east4)
#
# Prereqs:
#   1. Install Railway CLI:  brew install railway   (or curl -fsSL https://railway.com/install.sh | sh)
#   2. railway login                                  (opens browser, one-time)
#   3. git init this directory + at least one commit (Railway deploys what git sees)

set -euo pipefail

# ---- helpers ---------------------------------------------------------------
RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
say()  { printf "%s▸%s %s\n" "$CYAN" "$RESET" "$*"; }
ok()   { printf "%s✓%s %s\n" "$GREEN" "$RESET" "$*"; }
warn() { printf "%s!%s %s\n" "$YELLOW" "$RESET" "$*"; }
die()  { printf "%s✗ %s%s\n" "$RED" "$*" "$RESET" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }

prompt_if_unset() {
    local var="$1" prompt="$2" secret="${3:-no}"
    if [ -z "${!var:-}" ]; then
        if [ "$secret" = "yes" ]; then
            read -rsp "$prompt: " val; echo
        else
            read -rp "$prompt: " val
        fi
        printf -v "$var" "%s" "$val"
        export "$var"
    fi
}

# ---- prereqs ---------------------------------------------------------------
need railway
need git

cd "$(dirname "$0")"

if [ ! -d .git ]; then
    say "initializing git repo (Railway deploys what git sees)"
    git init -q
    git add .
    git -c user.email=deploy@local -c user.name=deploy commit -q -m "initial commit"
fi

# ---- auth check ------------------------------------------------------------
if ! railway whoami >/dev/null 2>&1; then
    warn "not logged in to Railway"
    say "running: railway login"
    railway login
fi
ok "Railway CLI authenticated"

# ---- gather config ---------------------------------------------------------
if [ "${1:-}" != "--non-interactive" ]; then
    prompt_if_unset PKB_KB_GIT_REMOTE \
        "Git URL of your KB repo (https://x:GITHUB_TOKEN@github.com/you/notes.git)"
    : "${PKB_KB_GIT_BRANCH:=main}"
    if [ -z "${PKB_API_KEY:-}" ]; then
        # Generate a strong key if none provided.
        PKB_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
        warn "no PKB_API_KEY provided — generated one:"
        printf "        %s%s%s\n" "$BOLD" "$PKB_API_KEY" "$RESET"
        warn "save this somewhere safe; you'll set it in every MCP client."
    fi
fi
: "${PKB_RAILWAY_SERVICE:=pkb}"

# ---- project + service -----------------------------------------------------
if [ -f .railway/project.json ] || railway status >/dev/null 2>&1; then
    ok "linked to an existing Railway project"
else
    if [ -n "${PKB_RAILWAY_PROJECT:-}" ]; then
        say "linking to existing project: $PKB_RAILWAY_PROJECT"
        railway link --project "$PKB_RAILWAY_PROJECT"
    else
        say "creating new Railway project: pkb"
        railway init --name pkb
    fi
fi

# Create the service if it doesn't exist (idempotent-ish; ignore if it already does).
if ! railway service "$PKB_RAILWAY_SERVICE" >/dev/null 2>&1; then
    say "creating service: $PKB_RAILWAY_SERVICE"
    railway add --service "$PKB_RAILWAY_SERVICE" || true
fi
railway service "$PKB_RAILWAY_SERVICE" >/dev/null 2>&1 || true

# ---- persistent volume -----------------------------------------------------
# Railway volumes survive redeploys. We mount at /data — matches the Dockerfile.
say "ensuring persistent volume at /data"
if ! railway volume list 2>/dev/null | grep -q "pkb-data"; then
    railway volume add --name pkb-data --mount-path /data --service "$PKB_RAILWAY_SERVICE" || \
        warn "could not auto-create volume — add via dashboard: Settings → Volumes → Mount /data"
else
    ok "volume pkb-data already present"
fi

# ---- environment variables -------------------------------------------------
say "setting environment variables"
declare -a VARS=(
    "PKB_TRANSPORT=sse"
    "PKB_DATA_DIR=/data"
    "PKB_KB_ROOT=/data/notes"
    "PKB_DB_PATH=/data/kb.db"
    "PKB_CACHE_DIR=/data/.fastembed_cache"
    "PKB_RERANK=true"
    "PKB_API_KEY=$PKB_API_KEY"
    "PKB_KB_GIT_BRANCH=$PKB_KB_GIT_BRANCH"
)
[ -n "${PKB_KB_GIT_REMOTE:-}" ] && VARS+=("PKB_KB_GIT_REMOTE=$PKB_KB_GIT_REMOTE")

for kv in "${VARS[@]}"; do
    railway variables --set "$kv" --service "$PKB_RAILWAY_SERVICE" >/dev/null
done
ok "${#VARS[@]} variables set"

# ---- deploy ----------------------------------------------------------------
say "deploying (this builds the Docker image — first run ~3-5 min)"
railway up --service "$PKB_RAILWAY_SERVICE" --detach

# ---- expose a public domain ------------------------------------------------
say "ensuring a public domain"
DOMAIN_OUT="$(railway domain --service "$PKB_RAILWAY_SERVICE" 2>&1 || true)"
DOMAIN="$(printf "%s" "$DOMAIN_OUT" | grep -oE 'https?://[A-Za-z0-9._-]+' | head -n1 || true)"
if [ -z "$DOMAIN" ]; then
    warn "no public domain found — generating one"
    railway domain --service "$PKB_RAILWAY_SERVICE" >/dev/null || true
    DOMAIN="$(railway domain --service "$PKB_RAILWAY_SERVICE" 2>&1 | grep -oE 'https?://[A-Za-z0-9._-]+' | head -n1 || true)"
fi
[ -n "$DOMAIN" ] && ok "public URL: $DOMAIN" || warn "couldn't read the domain — check the Railway dashboard"

# ---- summary ---------------------------------------------------------------
cat <<EOF

${BOLD}deployment kicked off.${RESET}

  service:        ${PKB_RAILWAY_SERVICE}
  URL:            ${DOMAIN:-<pending>}
  health:         ${DOMAIN:-...}/healthz
  MCP SSE:        ${DOMAIN:-...}/sse
  webhook sync:   ${DOMAIN:-...}/webhook/sync   (POST, requires bearer)

next steps:
  1. wait ~3-5 min for the first build to finish.
     check progress:  railway logs --service ${PKB_RAILWAY_SERVICE}
  2. once /healthz returns 200, point your MCP clients at the SSE URL.
     see docs/AGENT_INTEGRATION.md for client configs.
  3. trigger a sync from your laptop after writing notes:
     curl -X POST -H "Authorization: Bearer \$PKB_API_KEY" ${DOMAIN:-...}/webhook/sync

your API key:
  ${BOLD}${PKB_API_KEY}${RESET}
EOF
