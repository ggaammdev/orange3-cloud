#!/usr/bin/env bash
# Set up (or check) the Azure test subscription used for orange3-cloud development.
#
# Uses the Azure CLI's sign-in (az login) and its default subscription, or the
# one given. Safe to run again: every step checks the current state first and
# changes only what is missing. Private details (the subscription ID) are
# written to .orange3-cloud-dev.env, which git ignores, and never to this
# script. It:
#   1. checks the subscription,
#   2. writes the subscription ID and region to the env file,
#   3. registers the resource providers Microsoft.Compute, Microsoft.Network,
#      Microsoft.Storage and Microsoft.Quota (for step 5),
#   4. creates a monthly cost budget (default 50, in the subscription's billing
#      currency) with email alerts at 50%, 90% and 100% of actual spend,
#   5. requests the vCPU quotas of the machine sizes in the region (the Dsv6 and
#      Esv6 families and the regional total, default 32 each) where they are
#      lower and no request is in progress. Azure decides some requests at
#      once and sends others to a support request in the portal,
#   6. checks that the region offers the machine sizes to this subscription.
#
# Usage:
#   scripts/setup-azure-test-subscription.sh [--subscription ID] [--email ADDRESS]
#                                            [--budget-amount N] [--vcpus N]
#
# The email for budget alerts is needed only when the budget is created.
# Environment overrides: AZURE_TEST_SUBSCRIPTION, AZURE_TEST_REGION (eastus),
# BUDGET_EMAIL, BUDGET_AMOUNT (50), VCPUS (32).

set -euo pipefail
# The Azure CLI's own Python prints warnings that aren't about this script.
export PYTHONWARNINGS=ignore

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.orange3-cloud-dev.env"
# shellcheck source=scripts/env-file.sh
source "$ROOT/scripts/env-file.sh"
SUBSCRIPTION="${AZURE_TEST_SUBSCRIPTION:-}"
REGION="${AZURE_TEST_REGION:-eastus}"
BUDGET_EMAIL="${BUDGET_EMAIL:-}"
BUDGET_AMOUNT="${BUDGET_AMOUNT:-50}"
VCPUS="${VCPUS:-32}"
BUDGET_NAME="orange3-cloud-test"
PROVIDERS=(Microsoft.Compute Microsoft.Network Microsoft.Storage Microsoft.Quota)
QUOTAS=(StandardDsv6Family StandardEsv6Family cores)
SIZES=(D4s_v6 D16s_v6 E16s_v6 D32s_v6 E32s_v6 D64s_v6 E64s_v6 D96s_v6 E96s_v6)
ARM="https://management.azure.com"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subscription) SUBSCRIPTION="$2"; shift 2 ;;
    --email) BUDGET_EMAIL="$2"; shift 2 ;;
    --budget-amount) BUDGET_AMOUNT="$2"; shift 2 ;;
    --vcpus) VCPUS="$2"; shift 2 ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

CHANGED=0
say() { printf '%s\n' "$*"; }
did() { CHANGED=1; printf '  changed: %s\n' "$*"; }
ok() { printf '  ok: %s\n' "$*"; }

[[ -n "$SUBSCRIPTION" ]] || SUBSCRIPTION="$(env_get ORANGE3_CLOUD_AZURE_SUBSCRIPTION)"

say "1. Subscription"
if [[ -z "$SUBSCRIPTION" ]] && ! SUBSCRIPTION="$(az account show --query id -o tsv 2>/dev/null)"; then
  echo "Not signed in. Run: az login" >&2
  exit 1
fi
if ! STATE="$(az account show --subscription "$SUBSCRIPTION" --query state -o tsv 2>/dev/null)"; then
  echo "The subscription isn't available to your sign-in. Run: az login" >&2
  exit 1
fi
[[ "$STATE" == Enabled ]] || { echo "The subscription is $STATE." >&2; exit 1; }
ok "subscription enabled"

say "2. $ENV_FILE"
if env_set ORANGE3_CLOUD_AZURE_SUBSCRIPTION "$SUBSCRIPTION"; then did "set ORANGE3_CLOUD_AZURE_SUBSCRIPTION"; fi
if env_set ORANGE3_CLOUD_AZURE_REGION "$REGION"; then did "set ORANGE3_CLOUD_AZURE_REGION"; fi
ok "env file up to date"

say "3. Resource providers"
for provider in "${PROVIDERS[@]}"; do
  if [[ "$(az provider show --subscription "$SUBSCRIPTION" -n "$provider" --query registrationState -o tsv)" == Registered ]]; then
    ok "$provider"
  else
    az provider register --subscription "$SUBSCRIPTION" -n "$provider" --wait >/dev/null
    did "registered $provider"
  fi
done

say "4. Budget"
BUDGET_URL="$ARM/subscriptions/$SUBSCRIPTION/providers/Microsoft.Consumption/budgets/$BUDGET_NAME?api-version=2023-05-01"
if az rest --method get --url "$BUDGET_URL" >/dev/null 2>&1; then
  ok "budget exists"
else
  [[ -n "$BUDGET_EMAIL" ]] || { echo "Give the email address for budget alerts with --email." >&2; exit 1; }
  alert() {
    printf '"actual%s": {"enabled": true, "operator": "GreaterThan", "threshold": %s,' "$1" "$1"
    printf ' "thresholdType": "Actual", "contactEmails": ["%s"]}' "$BUDGET_EMAIL"
  }
  az rest --method put --url "$BUDGET_URL" --body "{\"properties\": {
      \"category\": \"Cost\", \"amount\": $BUDGET_AMOUNT, \"timeGrain\": \"Monthly\",
      \"timePeriod\": {\"startDate\": \"$(date -u +%Y-%m-01)T00:00:00Z\"},
      \"notifications\": {$(alert 50), $(alert 90), $(alert 100)}}}" >/dev/null
  did "created a budget of $BUDGET_AMOUNT a month with alerts at 50%, 90%, 100%"
fi

say "5. vCPU quotas in $REGION"
QUOTA_SCOPE="$ARM/subscriptions/$SUBSCRIPTION/providers/Microsoft.Compute/locations/$REGION/providers/Microsoft.Quota"
QUOTA_API="api-version=2023-02-01"
last_request() {  # quota name; prints the state of the newest request for it, if any
  az rest --method get --url "$QUOTA_SCOPE/quotaRequests?$QUOTA_API" -o json 2>/dev/null | python3 -c '
import json, sys
name = sys.argv[1].lower()  # Azure answers with the names in capitals
requests = []
for request in json.load(sys.stdin).get("value", []):
    props = request.get("properties") or {}
    for part in props.get("value") or []:
        if (part.get("name") or {}).get("value", "").lower() == name:
            state = part.get("provisioningState") or props.get("provisioningState")
            requests.append((props.get("requestSubmitTime", ""), state))
if requests:
    print(max(requests)[1])' "$1"
}
for quota in "${QUOTAS[@]}"; do
  LIMIT="$(az rest --method get --url "$QUOTA_SCOPE/quotas/$quota?$QUOTA_API" --query properties.limit.value -o tsv)"
  LIMIT="${LIMIT%.*}"
  LAST="$(last_request "$quota")"
  if (( LIMIT >= VCPUS )); then
    ok "$quota: $LIMIT"
  elif [[ "$LAST" =~ ^(Accepted|Created|InProgress)$ ]]; then
    ok "$quota: $LIMIT, and a request is being processed"
  elif [[ "$LAST" == Failed ]]; then
    say "  needs you: $quota is $LIMIT, and Azure didn't approve the request at once. Request $VCPUS in the Azure portal (Quotas, Compute, $REGION), which opens a support request."
  elif ERROR="$(az rest --method put --url "$QUOTA_SCOPE/quotas/$quota?$QUOTA_API" --body "{\"properties\": {
      \"limit\": {\"limitObjectType\": \"LimitValue\", \"value\": $VCPUS}, \"name\": {\"value\": \"$quota\"}}}" 2>&1 >/dev/null)"; then
    did "requested $VCPUS for $quota (now $LIMIT)"
  else
    say "  not requested: $quota ($LIMIT). Request $VCPUS in the Azure portal (Quotas, Compute, $REGION). Azure said: ${ERROR//$'\n'/ }"
  fi
done

say "6. Machine sizes offered in $REGION"
GENERATION="_${SIZES[0]##*_}"  # e.g. "_v6"
SKUS="$(az vm list-skus --subscription "$SUBSCRIPTION" --location "$REGION" --resource-type virtualMachines \
  --query "[?ends_with(name, '$GENERATION')].[name, length(restrictions)]" -o tsv)"
for size in "${SIZES[@]}"; do
  name="Standard_$size"
  if ! grep -q "^$name	" <<<"$SKUS"; then
    say "  not offered: $name"
  elif ! grep -q "^$name	0$" <<<"$SKUS"; then
    say "  restricted for this subscription: $name"
  else
    ok "$name"
  fi
done

if [[ $CHANGED -eq 0 ]]; then
  say "Done: nothing changed."
else
  say "Done: changes made (see above)."
fi
