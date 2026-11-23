#!/usr/bin/env bash
# Set up (or check) the AWS test account used for orange3-cloud development.
#
# Uses the AWS CLI profile "orange3-cloud". Sign in first:
#   aws login --profile orange3-cloud --region us-east-1
#
# Safe to run again: every step checks the current state first and changes
# only what is missing. Private details (the account ID) are written to
# .orange3-cloud-dev.env, which git ignores, and never to this script. It:
#   1. checks the profile,
#   2. writes the profile, account ID and region to the env file,
#   3. creates a monthly cost budget (default 50 USD) with email alerts at
#      50%, 90% and 100% of actual spend,
#   4. requests the EC2 vCPU quota for standard instances in the region
#      (default 32) if it is lower and no request is open.
#
# Usage:
#   scripts/setup-aws-test-account.sh [--email ADDRESS] [--budget-amount N] [--vcpus N]
#
# The email for budget alerts is needed only when the budget is created.
# Environment overrides: AWS_TEST_PROFILE (orange3-cloud), AWS_TEST_REGION
# (us-east-1), BUDGET_EMAIL, BUDGET_AMOUNT (50), VCPUS (32).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.orange3-cloud-dev.env"
# shellcheck source=scripts/env-file.sh
source "$ROOT/scripts/env-file.sh"
PROFILE="${AWS_TEST_PROFILE:-orange3-cloud}"
REGION="${AWS_TEST_REGION:-us-east-1}"
BUDGET_EMAIL="${BUDGET_EMAIL:-}"
BUDGET_AMOUNT="${BUDGET_AMOUNT:-50}"
VCPUS="${VCPUS:-32}"
BUDGET_NAME="orange3-cloud test account"
# "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances", in vCPUs.
QUOTA_CODE="L-1216C47A"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email) BUDGET_EMAIL="$2"; shift 2 ;;
    --budget-amount) BUDGET_AMOUNT="$2"; shift 2 ;;
    --vcpus) VCPUS="$2"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

CHANGED=0
say() { printf '%s\n' "$*"; }
did() { CHANGED=1; printf '  changed: %s\n' "$*"; }
ok() { printf '  ok: %s\n' "$*"; }
a() { aws --profile "$PROFILE" --region "$REGION" "$@"; }

say "1. Profile '$PROFILE'"
if ! ACCOUNT_ID="$(a sts get-caller-identity --query Account --output text 2>/dev/null)"; then
  echo "The profile doesn't work. Sign in with: aws login --profile $PROFILE --region $REGION" >&2
  exit 1
fi
ok "signed in"

say "2. $ENV_FILE"
if env_set ORANGE3_CLOUD_AWS_PROFILE "$PROFILE"; then did "set ORANGE3_CLOUD_AWS_PROFILE"; fi
if env_set ORANGE3_CLOUD_AWS_ACCOUNT "$ACCOUNT_ID"; then did "set ORANGE3_CLOUD_AWS_ACCOUNT"; fi
if env_set ORANGE3_CLOUD_AWS_REGION "$REGION"; then did "set ORANGE3_CLOUD_AWS_REGION"; fi
ok "env file up to date"

say "3. Budget"
if a budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" >/dev/null 2>&1; then
  ok "budget exists"
else
  [[ -n "$BUDGET_EMAIL" ]] || { echo "Give the email address for budget alerts with --email." >&2; exit 1; }
  alert() {
    printf '{"Notification": {"NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN",'
    printf ' "Threshold": %s, "ThresholdType": "PERCENTAGE"},' "$1"
    printf ' "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "%s"}]}' "$BUDGET_EMAIL"
  }
  a budgets create-budget --account-id "$ACCOUNT_ID" \
    --budget "{\"BudgetName\": \"$BUDGET_NAME\", \"BudgetType\": \"COST\", \"TimeUnit\": \"MONTHLY\",
               \"BudgetLimit\": {\"Amount\": \"$BUDGET_AMOUNT\", \"Unit\": \"USD\"}}" \
    --notifications-with-subscribers "[$(alert 50), $(alert 90), $(alert 100)]"
  did "created a budget of $BUDGET_AMOUNT USD a month with alerts at 50%, 90%, 100%"
fi

say "4. EC2 vCPU quota for standard instances in $REGION"
CURRENT="$(a service-quotas get-service-quota --service-code ec2 --quota-code "$QUOTA_CODE" \
  --query 'Quota.Value' --output text)"
CURRENT="${CURRENT%.*}"
if (( CURRENT >= VCPUS )); then
  ok "$CURRENT vCPUs"
else
  OPEN="$(a service-quotas list-requested-service-quota-change-history-by-quota \
    --service-code ec2 --quota-code "$QUOTA_CODE" \
    --query "RequestedQuotas[?Status=='PENDING' || Status=='CASE_OPENED'].DesiredValue | [0]" --output text)"
  if [[ -n "$OPEN" && "$OPEN" != "None" ]]; then
    ok "$CURRENT vCPUs, and a request for ${OPEN%.*} is open"
  else
    a service-quotas request-service-quota-increase --service-code ec2 --quota-code "$QUOTA_CODE" \
      --desired-value "$VCPUS" >/dev/null
    did "requested $VCPUS vCPUs (now $CURRENT). AWS answers by email, often within a day."
  fi
fi

if [[ $CHANGED -eq 0 ]]; then
  say "Done: nothing changed."
else
  say "Done: changes made (see above)."
fi
