#!/usr/bin/env bash
#
# validate.sh - everything CI checks, runnable locally before a push.
#
# Needs promtool, amtool and python3 (with pyyaml) on PATH.
# Rule files and targets are referenced by their /etc paths in the configs,
# so each config is checked inside a temp dir laid out like the server.

set -euo pipefail
cd "$(dirname "$0")/.."

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail=0
step() { echo; echo "== $*"; }

step "Prometheus rule files"
promtool check rules prometheus/rules/recording.yml prometheus/rules/alerts/*.yml || fail=1

step "Rule unit tests"
promtool test rules tests/rules/*_test.yml || fail=1

step "Prometheus configs"
for cfg in prometheus/environments/*/prometheus.yml prometheus/global/prometheus.yml; do
    root="$tmp/$(echo "$cfg" | tr / _)"
    mkdir -p "$root/rules/alerts" "$root/targets"
    cp prometheus/rules/recording.yml "$root/rules/"
    cp prometheus/rules/alerts/*.yml "$root/rules/alerts/"
    env_dir=$(dirname "$cfg")
    [[ -d "$env_dir/targets" ]] && cp "$env_dir"/targets/*.yml "$root/targets/"
    sed "s|/etc/prometheus|$root|g" "$cfg" > "$root/prometheus.yml"
    echo "-- $cfg"
    promtool check config "$root/prometheus.yml" | tail -n +2 || fail=1
done

step "Alertmanager config and routing"
amtool check-config alertmanager/alertmanager.yml >/dev/null && echo "  SUCCESS" || fail=1
bash tests/alertmanager/routes.sh || fail=1

step "Generated Kubernetes rules are up to date"
python3 scripts/build-k8s-rules.py --check || fail=1

step "Dispatcher unit tests"
( cd dispatcher && python3 -m unittest -q test_alert_dispatcher 2>&1 | tail -3 ) || fail=1

step "Grafana dashboards are valid JSON"
for f in grafana/dashboards/*.json; do
    python3 -m json.tool "$f" >/dev/null && echo "  ok  $f" || { echo "  BAD $f"; fail=1; }
done

echo
if (( fail )); then echo "Some checks failed."; exit 1; fi
echo "All checks passed."
