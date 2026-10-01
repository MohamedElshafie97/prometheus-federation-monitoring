#!/usr/bin/env bash
#
# Routing tests for alertmanager.yml: each line is "labels => expected receiver".
# A routing change that sends production criticals somewhere else fails here
# instead of being discovered during an outage.

set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1

cases=(
    "env=production severity=critical alertname=HostDown           => prod-critical"
    "env=production severity=critical alertname=MySQLDown          => prod-critical"
    "env=production severity=warning alertname=HostDiskSpaceLow    => chat-prod"
    "env=staging severity=critical alertname=HostDown              => chat-nonprod"
    "env=dev severity=warning alertname=KubePodCrashLooping        => chat-nonprod"
    "alertname=Watchdog severity=none                              => heartbeat"
    "severity=warning alertname=SomethingWithoutEnv                => chat-nonprod"
)

fail=0
for c in "${cases[@]}"; do
    labels=${c%%=>*}
    want=$(echo "${c##*=>}" | xargs)
    # shellcheck disable=SC2086
    got=$(amtool config routes test --config.file=alertmanager/alertmanager.yml $labels 2>/dev/null)
    if [[ "$got" == "$want" ]]; then
        printf '  ok    %-60s -> %s\n' "$(echo $labels | xargs)" "$got"
    else
        printf '  FAIL  %-60s -> %s (expected %s)\n' "$(echo $labels | xargs)" "$got" "$want"
        fail=1
    fi
done
exit $fail
