#!/usr/bin/env bash
# XeroFlow installer
# Usage:
#   bash install.sh --mode local --repo https://github.com/oodog/Xero-AzureAi-receipts.git --dir xeroflow
#   bash install.sh --mode azure-webapp --app-name <app> --resource-group <rg> --location <loc> --repo <url>
#
# Intended for: curl -sS https://raw.githubusercontent.com/<user>/<repo>/main/scripts/install.sh | bash -s -- [flags]
#
set -euo pipefail

MODE="local"
REPO_URL="https://github.com/oodog/Xero-AzureAi-receipts.git"
WORKDIR="xeroflow"
APP_NAME=""
RESOURCE_GROUP=""
LOCATION="australiaeast"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log() { printf "\033[1;34m[INFO]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[ERR ]\033[0m %s\n" "$*" >&2; }
die() { err "$*"; exit 1; }

usage() {
  cat <<EOF
XeroFlow installer

Flags:
  --mode local|azure-webapp     Install mode (default: local)
  --repo <git url>              Git repo to clone (default: $REPO_URL)
  --dir <folder>                Folder to clone into (default: $WORKDIR)

Azure (for --mode azure-webapp):
  --app-name <name>             App Service web app name
  --resource-group <rg>         Azure resource group
  --location <loc>              Azure location (default: $LOCATION)

Environment variables respected:
  PYTHON_BIN          Python executable to use (default: python3)
  AZ_SUBSCRIPTION     Subscription id/name for az cli (optional)
  NON_INTERACTIVE=1   Skip prompts; fail if required values missing

Examples:
  curl -sS https://raw.githubusercontent.com/<user>/<repo>/main/scripts/install.sh | bash -s -- --mode local
  curl -sS https://raw.githubusercontent.com/<user>/<repo>/main/scripts/install.sh | bash -s -- --mode azure-webapp --app-name my-xeroflow --resource-group rg-xeroflow
EOF
}

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mode) MODE="${2:-}"; shift 2;;
      --repo) REPO_URL="${2:-}"; shift 2;;
      --dir) WORKDIR="${2:-}"; shift 2;;
      --app-name) APP_NAME="${2:-}"; shift 2;;
      --resource-group) RESOURCE_GROUP="${2:-}"; shift 2;;
      --location) LOCATION="${2:-}"; shift 2;;
      -h|--help) usage; exit 0;;
      *) die "Unknown flag: $1 (use --help)";;
    esac
  done
}

clone_repo() {
  need_cmd git
  if [[ -d "$WORKDIR/.git" ]]; then
    log "Repo already present in $WORKDIR — pulling latest..."
    (cd "$WORKDIR" && git pull --rebase --autostash)
  else
    log "Cloning $REPO_URL into $WORKDIR ..."
    git clone --depth=1 "$REPO_URL" "$WORKDIR"
  fi
}

ensure_python() {
  need_cmd "$PYTHON_BIN"
  "$PYTHON_BIN" - <<'PY' || die "Python 3.9+ is required."
import sys
maj, min = sys.version_info[:2]
assert (maj, min) >= (3, 9)
PY
}

create_venv_and_install() {
  ensure_python
  cd "$WORKDIR"
  if [[ ! -d ".venv" ]]; then
    log "Creating virtualenv (.venv) ..."
    "$PYTHON_BIN" -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install --upgrade pip wheel
  if [[ -f "requirements.txt" ]]; then
    log "Installing Python dependencies ..."
    pip install -r requirements.txt
  else
    warn "requirements.txt not found — skipping pip install"
  fi
}

make_env_file() {
  cd "$WORKDIR"
  if [[ -f ".env" ]]; then
    log ".env already exists — leaving it as-is"
    return
  fi

  if [[ -f ".env.example" ]]; then
    log "Creating .env from .env.example ..."
    cp .env.example .env
  else
    warn ".env.example missing — creating a minimal .env"
    cat > .env <<'EOF'
FLASK_SECRET_KEY=
SESSION_TYPE=filesystem

AZURE_STORAGE_ACCOUNT_NAME=
AZURE_STORAGE_ACCOUNT_KEY=
AZURE_STORAGE_BLOB_ENDPOINT=

DOCUMENT_INTELLIGENCE_ENDPOINT=
AZURE_DI_ENDPOINT=
AZURE_DI_KEY=

COSMOS_DB_ENDPOINT=

KEY_VAULT_URL=

AZURE_SEARCH_ENDPOINT=
AZURE_SEARCH_API_KEY=
AZURE_SEARCH_INDEX=receipts

# Embeddings (Azure OpenAI preferred)
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_API_VERSION=2024-06-01
AZURE_OPENAI_EMBEDDING_MODEL=text-embedding-3-small
EOF
  fi

  # Generate a secret if empty
  if grep -q '^FLASK_SECRET_KEY=$' .env; then
    log "Generating FLASK_SECRET_KEY ..."
    python - <<'PY'\
import secrets; print(f"FLASK_SECRET_KEY={secrets.token_urlsafe(32)}")\
PY
  else
    # Print the line as-is (no change)
    grep '^FLASK_SECRET_KEY=' .env | head -n1
  fi | tee /tmp/.env.tmp >/dev/null

  # Merge/replace FLASK_SECRET_KEY line
  if grep -q '^FLASK_SECRET_KEY=' /tmp/.env.tmp; then
    awk 'BEGIN{FS=OFS="="} NR==FNR{a[$1]=$0;next} $1 in a{$0=a[$1]}1' /tmp/.env.tmp .env > .env.new && mv .env.new .env
    rm -f /tmp/.env.tmp
  fi
}

print_next_steps_local() {
  cat <<'TXT'

✅ Local install complete.

Next steps:
  cd '"$WORKDIR"'
  source .venv/bin/activate
  # Configure your .env (endpoints/keys) before running:
  #   - AZURE_STORAGE_ACCOUNT_NAME / KEY (or use RBAC for user-delegation SAS)
  #   - DOCUMENT_INTELLIGENCE_ENDPOINT (or AZURE_DI_ENDPOINT) (+ key if not using AAD)
  #   - COSMOS_DB_ENDPOINT
  #   - KEY_VAULT_URL
  #   - AZURE_SEARCH_ENDPOINT / AZURE_SEARCH_API_KEY
  #   - Embeddings: AZURE_OPENAI_* or OPENAI_API_KEY

  # Start the app (dev):
  flask --app main_web_app run --port 5000

  # Or with gunicorn (prod-like):
  gunicorn main_web_app:app --workers 3 --threads 8 --timeout 180
TXT
}

azure_webapp_setup() {
  need_cmd az

  [[ -n "${APP_NAME}" ]] || die "--app-name is required for --mode azure-webapp"
  [[ -n "${RESOURCE_GROUP}" ]] || die "--resource-group is required for --mode azure-webapp"

  if [[ -n "${AZ_SUBSCRIPTION:-}" ]]; then
    log "Setting subscription: $AZ_SUBSCRIPTION"
    az account set --subscription "$AZ_SUBSCRIPTION"
  fi

  log "Ensuring resource group ..."
  az group create --name "$RESOURCE_GROUP" --location "$LOCATION" >/dev/null

  log "Creating App Service Plan (Linux, B1) if needed ..."
  az appservice plan create --name "${APP_NAME}-plan" --resource-group "$RESOURCE_GROUP" --is-linux --sku B1 >/dev/null || true

  log "Creating Web App (Python 3.10) if needed ..."
  az webapp create \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --plan "${APP_NAME}-plan" \
    --runtime "PYTHON:3.10" >/dev/null || true

  log "Configuring startup command (gunicorn) ..."
  az webapp config set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --startup-file "gunicorn main_web_app:app --workers 3 --threads 8 --timeout 180" >/dev/null

  # Push environment - these must be supplied as env vars before invoking this mode, or edit in Azure Portal later.
  log "Pushing minimal app settings (you should add all required secrets/endpoints afterward) ..."
  az webapp config appsettings set --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --settings \
    FLASK_SECRET_KEY="$(python - <<'PY'\
import secrets; print(secrets.token_urlsafe(32))\
PY
)" >/dev/null

  log "Zipping and deploying current repo snapshot ..."
  (cd "$WORKDIR" && zip -qr ../deploy.zip .)
  az webapp deployment source config-zip --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --src deploy.zip >/dev/null
  rm -f deploy.zip

  cat <<TXT

✅ Azure Web App deployment kicked off.

IMPORTANT: Go to the Web App's Configuration and add the required settings:
  AZURE_STORAGE_ACCOUNT_NAME
  (AZURE_STORAGE_ACCOUNT_KEY if not using RBAC for user delegation SAS)
  DOCUMENT_INTELLIGENCE_ENDPOINT or AZURE_DI_ENDPOINT (and AZURE_DI_KEY if not using AAD)
  COSMOS_DB_ENDPOINT
  KEY_VAULT_URL
  AZURE_SEARCH_ENDPOINT / AZURE_SEARCH_API_KEY
  (Azure OpenAI or OpenAI credentials for embeddings)

Visit: https://portal.azure.com/#resource/subscriptions/<sub>/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.Web/sites/${APP_NAME}/configuration
TXT
}

main() {
  parse_args "$@"

  case "$MODE" in
    local)
      clone_repo
      create_venv_and_install
      make_env_file
      print_next_steps_local
      ;;
    azure-webapp)
      clone_repo
      create_venv_and_install   # builds your dependencies to ensure requirements are valid
      azure_webapp_setup
      ;;
    *)
      die "Unknown mode: $MODE"
      ;;
  esac
}

main "$@"
