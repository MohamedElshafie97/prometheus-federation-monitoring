# Runbook

What to check first when one of the alerts fires. Each heading matches the `runbook` annotation on the alert.

## FederationTargetDown

The global Prometheus can't scrape `/federate` on one environment Prometheus. Every alert for that environment is blind until this is fixed, which is why Alertmanager suppresses that environment's other alerts while this one fires.

1. From the global server: `curl -s -o /dev/null -w '%{http_code}\n' http://<env-prom>:9090/-/ready`
2. Not reachable: check the host is up and port 9090 is open from the global server (`firewall-cmd --list-all`).
3. Reachable but not ready: `journalctl -u prometheus -n 100` on the env server. A WAL replay after a crash can take minutes on a big TSDB, so let it finish.
4. For a Kubernetes environment: `kubectl -n monitoring get pods,svc` and check the NodePort `30090`.

## FederationScrapeSlow

The federation scrape is getting close to its timeout. Almost always a new recording rule that produces far more series than expected.

```bash
# On the env Prometheus: biggest recording rules by series count
curl -s http://localhost:9090/api/v1/status/tsdb | jq '.data.seriesCountByMetricName[] | select(.name | contains(":"))' | head -20
```

Fix the rule (aggregate away a high-cardinality label), don't raise the timeout.

## HostDown

1. Ping the host and try SSH.
2. Host is up: `systemctl status node_exporter` and `curl localhost:9100/metrics | head`.
3. Exporter is fine locally: check the firewall rule that allows the environment Prometheus to reach 9100.
4. Host is really down: check the hypervisor console before rebooting anything.

## HostDiskSpace

Covers `HostDiskSpaceLow`, `HostDiskSpaceCritical` and `HostDiskWillFillIn4Hours`.

```bash
df -h <mountpoint>
du -xh --max-depth=2 <mountpoint> 2>/dev/null | sort -rh | head -15
lsof +L1 | head    # deleted files still held open
```

Common causes: application logs without rotation, journald without a size cap, old kernels, Docker images and stopped containers (`docker system df`), MariaDB binlogs (`SHOW BINARY LOGS;`, then purge with care if replicas are caught up).

## MySQLDown

1. `systemctl status mariadb` and the error log.
2. `mysql_up` is 0 but MariaDB is running: the exporter can't log in. Check `/etc/.mysqld_exporter.cnf` and whether the `exporter` user still exists.
3. If it's a primary and won't come back, decide on failover before anything else. Check replica lag first.

## MySQLReplicationStopped

```sql
SHOW SLAVE STATUS\G
```

Look at `Slave_IO_Running`, `Slave_SQL_Running`, `Last_IO_Error`, `Last_SQL_Error` and `Gtid_IO_Pos`.

- **IO thread stopped**: network or credentials to the source. Check from the replica: `mysql -h <source> -u repl -p`.
- **SQL thread stopped**: a statement failed to apply, usually a duplicate key or a missing row. Find out why before doing anything. Skipping a GTID transaction silently leaves the replica different from the source.
- For the DR replica, also check the link between sites.

## AlertDispatcherErrors

Alerts reach the dispatcher but not Google Chat. Email still works for production criticals.

```bash
docker ps --filter name=alert-dispatcher     # running? healthy?
docker logs --tail 50 alert-dispatcher
curl -s localhost:9095/metrics | grep dispatcher_
```

- `404` / `400`: the webhook was deleted or regenerated in the Chat space. Update it in vault and redeploy the `dispatcher` role.
- `429`: too many messages. Look at what's flapping.
- Timeouts: outbound HTTPS from the alerting server is blocked.

## KubeNodeNotReady

```bash
kubectl get nodes
kubectl describe node <node>     # Conditions + Events
```

Then on the node: `systemctl status kubelet containerd` and `journalctl -u kubelet -n 100`. Disk pressure and memory pressure show up in the node conditions.
