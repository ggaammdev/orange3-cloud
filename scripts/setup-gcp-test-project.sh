#!/usr/bin/env bash
# Create (or check) the Google Cloud test project used for orange3-cloud development.
#
# Safe to run again: every step checks the current state first and changes
# only what is missing. Private details (project ID, billing account) are
# written to .orange3-cloud-dev.env, which git ignores, and never to this script.
#
# Usage:
#   scripts/setup-gcp-test-project.sh [--billing-account ID] [--budget-amount N] [--project ID]
#
# Environment overrides: BILLING_ACCOUNT, BUDGET_AMOUNT (default 50, in the
# billing account's currency), PROJECT_ID (default: from the env file, else a
# new orange3-cloud-test-<random>), REGION (us-central1), ZONE (us-central1-a).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.orange3-cloud-dev.env"
# shellcheck source=scripts/env-file.sh
source "$ROOT/scripts/env-file.sh"
CONFIG_NAME="orange3-cloud"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
BUDGET_AMOUNT="${BUDGET_AMOUNT:-50}"
BILLING_ACCOUNT="${BILLING_ACCOUNT:-}"
PROJECT_ID="${PROJECT_ID:-}"
APIS=(
  compute.googleapis.com
  storage.googleapis.com
  artifactregistry.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com
  sts.googleapis.com
  cloudresourcemanager.googleapis.com
  serviceusage.googleapis.com
  billingbudgets.googleapis.com
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --billing-account) BILLING_ACCOUNT="$2"; shift 2 ;;
    --budget-amount) BUDGET_AMOUNT="$2"; shift 2 ;;
    --project) PROJECT_ID="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

CHANGED=0
say() { printf '%s\n' "$*"; }
did() { CHANGED=1; printf '  changed: %s\n' "$*"; }
ok() { printf '  ok: %s\n' "$*"; }

# Read values from an earlier run, so a second run reuses the same project.
[[ -z "$PROJECT_ID" ]] && PROJECT_ID="$(env_get ORANGE3_CLOUD_GCP_PROJECT)"
[[ -z "$BILLING_ACCOUNT" ]] && BILLING_ACCOUNT="$(env_get ORANGE3_CLOUD_BILLING_ACCOUNT)"

ACCOUNT="$(gcloud config get-value account 2>/dev/null)"
[[ -n "$ACCOUNT" ]] || { echo "Not signed in. Run: gcloud auth login" >&2; exit 1; }

# Billing account: use the given one, or the only open one.
if [[ -z "$BILLING_ACCOUNT" ]]; then
  OPEN_ACCOUNTS="$(gcloud billing accounts list --filter=open=true --format='value(name.basename())')"
  if [[ -n "$OPEN_ACCOUNTS" && "$(wc -l <<<"$OPEN_ACCOUNTS" | tr -d ' ')" == 1 ]]; then
    BILLING_ACCOUNT="$OPEN_ACCOUNTS"
  else
    echo "Several (or no) open billing accounts. Choose one with --billing-account:" >&2
    gcloud billing accounts list --filter=open=true --format='table(name.basename(),displayName,currencyCode)' >&2
    exit 1
  fi
fi
CURRENCY="$(gcloud billing accounts describe "$BILLING_ACCOUNT" --format='value(currencyCode)')"

# 1. Project
say "1. Project"
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="orange3-cloud-test-$(openssl rand -hex 3)"
fi
if gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null 2>&1; then
  ok "project exists"
else
  gcloud projects create "$PROJECT_ID" --name="orange3-cloud test" --labels=orange3-cloud=true --quiet
  did "created project $PROJECT_ID"
fi

# 2. Private env file, written right away so a re-run after a failure reuses this project.
# Only this script's keys change; the other setup scripts' keys stay.
say "2. $ENV_FILE"
for pair in \
    "ORANGE3_CLOUD_GCP_PROJECT=$PROJECT_ID" \
    "ORANGE3_CLOUD_GCP_REGION=$REGION" \
    "ORANGE3_CLOUD_GCP_ZONE=$ZONE" \
    "ORANGE3_CLOUD_GCLOUD_CONFIG=$CONFIG_NAME" \
    "ORANGE3_CLOUD_BILLING_ACCOUNT=$BILLING_ACCOUNT" \
    "ORANGE3_CLOUD_BILLING_CURRENCY=$CURRENCY"; do
  if env_set "${pair%%=*}" "${pair#*=}"; then did "set ${pair%%=*}"; fi
done
ok "env file up to date"

# 3. Billing link
say "3. Billing"
LINKED="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingAccountName.basename())')"
if [[ "$LINKED" == "$BILLING_ACCOUNT" ]]; then
  ok "billing linked"
else
  gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT" --quiet >/dev/null
  did "linked billing account"
fi

# APIs come before the budget: the budget call is billed to this project,
# so the Billing Budgets API must be enabled in it first.
say "4. APIs"
ENABLED="$(gcloud services list --enabled --project="$PROJECT_ID" --format='value(config.name)')"
MISSING=()
for api in "${APIS[@]}"; do
  grep -qx "$api" <<<"$ENABLED" || MISSING+=("$api")
done
if [[ ${#MISSING[@]} -eq 0 ]]; then
  ok "all ${#APIS[@]} APIs enabled"
else
  gcloud services enable "${MISSING[@]}" --project="$PROJECT_ID" --quiet
  did "enabled ${MISSING[*]}"
fi

say "5. Budget"
BUDGET_NAME="orange3-cloud test project"
EXISTING="$(gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" \
  --billing-project="$PROJECT_ID" --format='value(displayName)' 2>/dev/null | grep -Fx "$BUDGET_NAME" || true)"
if [[ -n "$EXISTING" ]]; then
  ok "budget exists"
else
  gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" --billing-project="$PROJECT_ID" \
    --display-name="$BUDGET_NAME" \
    --budget-amount="${BUDGET_AMOUNT}${CURRENCY}" \
    --filter-projects="projects/$PROJECT_ID" \
    --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
    --quiet >/dev/null
  did "created budget of $BUDGET_AMOUNT $CURRENCY with alerts at 50%, 90%, 100%"
fi

# 6. Separate gcloud configuration; the user's active configuration stays untouched.
say "6. gcloud configuration '$CONFIG_NAME'"
if ! gcloud config configurations describe "$CONFIG_NAME" >/dev/null 2>&1; then
  gcloud config configurations create "$CONFIG_NAME" --no-activate --quiet >/dev/null 2>&1
  did "created configuration"
fi
set_prop() {
  local key="$1" value="$2" current
  current="$(gcloud --configuration="$CONFIG_NAME" config get-value "$key" 2>/dev/null || true)"
  if [[ "$current" != "$value" ]]; then
    gcloud --configuration="$CONFIG_NAME" config set "$key" "$value" --quiet >/dev/null 2>&1
    did "set $key"
  fi
}
set_prop core/account "$ACCOUNT"
set_prop core/project "$PROJECT_ID"
set_prop compute/region "$REGION"
set_prop compute/zone "$ZONE"
ok "configuration up to date"

if [[ $CHANGED -eq 0 ]]; then
  say "Done: nothing changed."
else
  say "Done: changes made (see above)."
fi
