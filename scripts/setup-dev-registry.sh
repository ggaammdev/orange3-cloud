#!/usr/bin/env bash
# Set up (or check) the private registry for development machine images.
#
# In the Google Cloud test project (see scripts/setup-gcp-test-project.sh) it
# creates:
#   - the Artifact Registry Docker repository "orange3-cloud",
#   - a service account that may push to that repository, and nothing else,
#   - read access to that repository for the project's default Compute Engine
#     service account, which development machines run as,
#   - a service account that may only read that repository, whose short-lived
#     tokens (made by the signed-in developer) let development machines on AWS
#     and Azure pull the image,
#   - keyless sign-in for GitHub Actions (Workload Identity Federation): a pool
#     and an OIDC provider that accept only pushes to main of this GitHub
#     repository, allowed to act as that service account.
# Then it stores the project, provider and service account as GitHub Actions
# secrets of the repository (masked in the workflow logs, which are public in a
# public repository) and the region as a variable; .github/workflows/image.yml
# reads them. Nothing private is written to the repository.
#
# Safe to run again: every step checks the current state first.
#
# Usage:
#   scripts/setup-dev-registry.sh [--github-repo OWNER/NAME]
#
# Reads ORANGE3_CLOUD_GCP_PROJECT, ORANGE3_CLOUD_GCP_REGION and
# ORANGE3_CLOUD_GCLOUD_CONFIG from .orange3-cloud-dev.env. The GitHub
# repository defaults to the one `gh repo view` shows.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.orange3-cloud-dev.env"
AR_REPO="orange3-cloud"
SA_NAME="orange3-cloud-dev-images"
READER_NAME="orange3-cloud-image-reader"
POOL="orange3-cloud-github"
PROVIDER="github"
GITHUB_REPO=""
APIS=(
  artifactregistry.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com
  sts.googleapis.com
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --github-repo) GITHUB_REPO="$2"; shift 2 ;;
    -h|--help) sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE: run scripts/setup-gcp-test-project.sh first." >&2; exit 1; }
env_value() { sed -n "s/^$1=//p" "$ENV_FILE"; }
PROJECT_ID="$(env_value ORANGE3_CLOUD_GCP_PROJECT)"
REGION="$(env_value ORANGE3_CLOUD_GCP_REGION)"
CONFIG_NAME="$(env_value ORANGE3_CLOUD_GCLOUD_CONFIG)"
[[ -n "$PROJECT_ID" && -n "$REGION" && -n "$CONFIG_NAME" ]] || { echo "Incomplete $ENV_FILE." >&2; exit 1; }
[[ -n "$GITHUB_REPO" ]] || GITHUB_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"

g() { gcloud --configuration="$CONFIG_NAME" --project="$PROJECT_ID" --quiet "$@"; }

CHANGED=0
say() { printf '%s\n' "$*"; }
did() { CHANGED=1; printf '  changed: %s\n' "$*"; }
ok() { printf '  ok: %s\n' "$*"; }

PROJECT_NUMBER="$(g projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SA_EMAIL="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
READER_EMAIL="$READER_NAME@$PROJECT_ID.iam.gserviceaccount.com"
POOL_NAME="projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL"
PROVIDER_NAME="$POOL_NAME/providers/$PROVIDER"
PRINCIPAL="principalSet://iam.googleapis.com/$POOL_NAME/attribute.repository/$GITHUB_REPO"
CONDITION="assertion.repository == '$GITHUB_REPO' && assertion.ref == 'refs/heads/main'"
MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref"

# Only APIs that the test project's setup already lists.
say "1. APIs"
ENABLED="$(g services list --enabled --format='value(config.name)')"
MISSING=()
for api in "${APIS[@]}"; do
  grep -qx "$api" <<<"$ENABLED" || MISSING+=("$api")
done
if [[ ${#MISSING[@]} -eq 0 ]]; then
  ok "all ${#APIS[@]} APIs enabled"
else
  g services enable "${MISSING[@]}"
  did "enabled ${MISSING[*]}"
fi

say "2. Artifact Registry repository '$AR_REPO'"
if g artifacts repositories describe "$AR_REPO" --location="$REGION" --format='value(name)' >/dev/null 2>&1; then
  ok "repository exists"
else
  g artifacts repositories create "$AR_REPO" --location="$REGION" --repository-format=docker \
    --description="orange3-cloud development machine images (private)" \
    --labels=orange3-cloud=true >/dev/null
  did "created repository"
fi

say "3. Service account '$SA_NAME'"
if g iam service-accounts describe "$SA_EMAIL" --format='value(email)' >/dev/null 2>&1; then
  ok "service account exists"
else
  g iam service-accounts create "$SA_NAME" \
    --display-name="orange3-cloud development images (GitHub Actions)" >/dev/null
  did "created service account"
  # A new service account can take a few seconds to be usable in IAM policies.
  sleep 10
fi

say "4. Push access to the repository (only)"
has_binding() {  # policy JSON on stdin; role, member
  python3 -c '
import json, sys
role, member = sys.argv[1], sys.argv[2]
policy = json.load(sys.stdin)
sys.exit(0 if any(b["role"] == role and member in b.get("members", [])
                  for b in policy.get("bindings", [])) else 1)' "$1" "$2"
}
if g artifacts repositories get-iam-policy "$AR_REPO" --location="$REGION" --format=json \
    | has_binding roles/artifactregistry.writer "serviceAccount:$SA_EMAIL"; then
  ok "service account can push"
else
  g artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" \
    --member="serviceAccount:$SA_EMAIL" --role=roles/artifactregistry.writer >/dev/null
  did "granted roles/artifactregistry.writer on the repository"
fi

say "4b. The machines' service account may pull from the repository"
# Development machines (orange3-cloud's Google Cloud provider) run as the
# project's default Compute Engine service account and pull the image as it.
VM_SA="$(g compute project-info describe --format='value(defaultServiceAccount)')"
if [[ -z "$VM_SA" ]]; then
  say "  skipped: the project has no default Compute Engine service account"
elif g artifacts repositories get-iam-policy "$AR_REPO" --location="$REGION" --format=json \
    | has_binding roles/artifactregistry.reader "serviceAccount:$VM_SA"; then
  ok "machines can pull"
else
  g artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" \
    --member="serviceAccount:$VM_SA" --role=roles/artifactregistry.reader >/dev/null
  did "granted roles/artifactregistry.reader on the repository to the machines' service account"
fi

say "4c. Service account '$READER_NAME' for machines on other clouds"
# Development machines on AWS and Azure can't use Google's service accounts,
# so they pull with a short-lived token of this account, which may only read
# the repository. Only the signed-in developer may make such tokens.
if g iam service-accounts describe "$READER_EMAIL" --format='value(email)' >/dev/null 2>&1; then
  ok "service account exists"
else
  g iam service-accounts create "$READER_NAME" \
    --display-name="orange3-cloud development images, read only (AWS and Azure machines)" >/dev/null
  did "created service account"
  sleep 10
fi
if g artifacts repositories get-iam-policy "$AR_REPO" --location="$REGION" --format=json \
    | has_binding roles/artifactregistry.reader "serviceAccount:$READER_EMAIL"; then
  ok "it can pull"
else
  g artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" \
    --member="serviceAccount:$READER_EMAIL" --role=roles/artifactregistry.reader >/dev/null
  did "granted roles/artifactregistry.reader on the repository"
fi
DEVELOPER="$(g config get-value account 2>/dev/null)"
if g iam service-accounts get-iam-policy "$READER_EMAIL" --format=json \
    | has_binding roles/iam.serviceAccountTokenCreator "user:$DEVELOPER"; then
  ok "you can make its tokens"
else
  g iam service-accounts add-iam-policy-binding "$READER_EMAIL" \
    --member="user:$DEVELOPER" --role=roles/iam.serviceAccountTokenCreator >/dev/null
  did "allowed you to make its short-lived tokens"
fi

say "5. Workload Identity pool '$POOL'"
POOL_STATE="$(g iam workload-identity-pools describe "$POOL" --location=global \
  --format='value(state)' 2>/dev/null || true)"
case "$POOL_STATE" in
  ACTIVE) ok "pool exists" ;;
  DELETED)
    g iam workload-identity-pools undelete "$POOL" --location=global >/dev/null
    did "restored the deleted pool" ;;
  *)
    g iam workload-identity-pools create "$POOL" --location=global \
      --display-name="orange3-cloud GitHub Actions" >/dev/null
    did "created pool" ;;
esac

say "6. OIDC provider '$PROVIDER' (only $GITHUB_REPO, branch main)"
CURRENT="$(g iam workload-identity-pools providers describe "$PROVIDER" --location=global \
  --workload-identity-pool="$POOL" --format='value(state,attributeCondition)' 2>/dev/null || true)"
if [[ -z "$CURRENT" ]]; then
  g iam workload-identity-pools providers create-oidc "$PROVIDER" --location=global \
    --workload-identity-pool="$POOL" --display-name="GitHub Actions" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION" >/dev/null
  did "created provider"
else
  if [[ "$CURRENT" == DELETED* ]]; then
    g iam workload-identity-pools providers undelete "$PROVIDER" --location=global \
      --workload-identity-pool="$POOL" >/dev/null
    did "restored the deleted provider"
  fi
  if [[ "${CURRENT#*$'\t'}" == "$CONDITION" ]]; then
    ok "provider exists with the right condition"
  else
    g iam workload-identity-pools providers update-oidc "$PROVIDER" --location=global \
      --workload-identity-pool="$POOL" \
      --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION" >/dev/null
    did "updated the provider's condition"
  fi
fi

say "7. GitHub Actions of $GITHUB_REPO may act as the service account"
if g iam service-accounts get-iam-policy "$SA_EMAIL" --format=json \
    | has_binding roles/iam.workloadIdentityUser "$PRINCIPAL"; then
  ok "binding exists"
else
  g iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
    --member="$PRINCIPAL" --role=roles/iam.workloadIdentityUser >/dev/null
  did "granted roles/iam.workloadIdentityUser"
fi

say "8. GitHub Actions secrets and variable"
set_var() {
  local name="$1" value="$2"
  if [[ "$(gh variable get "$name" --repo "$GITHUB_REPO" 2>/dev/null || true)" == "$value" ]]; then
    ok "variable $name"
  else
    gh variable set "$name" --repo "$GITHUB_REPO" --body "$value"
    did "set variable $name"
  fi
}
# A secret can't be read back to compare, so it is set every time (from
# stdin, so the value never appears in a command line).
set_secret() {
  local name="$1" value="$2"
  printf '%s' "$value" | gh secret set "$name" --repo "$GITHUB_REPO" >/dev/null
  ok "secret $name (set again)"
}
set_secret GCP_PROJECT "$PROJECT_ID"
set_secret GCP_WIF_PROVIDER "$PROVIDER_NAME"
set_secret GCP_DEV_IMAGES_SA "$SA_EMAIL"
set_var GCP_REGION "$REGION"

say "Development images: $REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO/machine:dev"
if [[ $CHANGED -eq 0 ]]; then
  say "Done: nothing changed."
else
  say "Done: changes made (see above)."
fi
