# Helpers for the private settings file .orange3-cloud-dev.env (git-ignored).
# Sourced by the setup scripts, which set ENV_FILE first.
#
#   env_get KEY        print KEY's value (nothing if it isn't set)
#   env_set KEY VALUE  set KEY and keep every other line; succeeds only if
#                      the file changed, so: if env_set K V; then did ...; fi

env_get() {
  [[ -f "$ENV_FILE" ]] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

env_set() {
  local key="$1" value="$2" tmp
  if [[ -f "$ENV_FILE" ]] && grep -q "^$key=" "$ENV_FILE" && [[ "$(env_get "$key")" == "$value" ]]; then
    return 1
  fi
  if [[ ! -f "$ENV_FILE" ]]; then
    printf '# Private development settings (git-ignored). Written by the scripts/setup-*.sh scripts.\n' >"$ENV_FILE"
  fi
  if grep -q "^$key=" "$ENV_FILE"; then
    tmp="$(mktemp)"
    KEY="$key" VALUE="$value" awk 'index($0, ENVIRON["KEY"] "=") == 1 { print ENVIRON["KEY"] "=" ENVIRON["VALUE"]; next } { print }' \
      "$ENV_FILE" >"$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
}
