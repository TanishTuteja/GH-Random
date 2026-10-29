# Remaining work

Reviewed 2026-10-29. This file contains unfinished operational work only.
Product enhancements belong in [ROADMAP.md](ROADMAP.md); completed production
checks are recorded in the [operations guide](deployment/OPERATIONS.md#live-acceptance--2026-10-28).

## Can do now

Access and free-capacity prerequisites are noted below; these tasks do not need
the catalog to age.

### OCI and network verification (console access)

- [ ] Read back the effective VCN security-list/NSG rules: public TCP 80/443,
  UDP 443 if HTTP/3 is enabled, and SSH only from the Bastion private endpoint.
  Confirm PostgreSQL, pgAdmin and Docker API have no public ingress. Check IPv6
  rules before publishing any AAAA record.
- [ ] Read back the Bastion client CIDR allowlist, session lifetime and private
  SSH ingress rule; verify they match the working restricted access path.
- [ ] Confirm tenancy-wide Always Free eligibility, quotas, storage/network
  usage and cost alerts. Count other resources, object versions and buckets.
  Budget notifications do not cap spending; keep the account free.
- [ ] Confirm whether the public IP is ephemeral or reserved and document the
  DuckDNS update/recovery procedure, with account recovery material kept private.

### Host and recovery rehearsals (SSH access)

- [ ] Verify the externally observed client address reaches the app through
  Caddy correctly, including rejection of spoofed forwarded headers.
- [ ] Exercise managed crawler interruption: confirm only the owned Docker
  child is cleaned up, the active catalog remains available and a subsequent
  normal crawl starts correctly. Use controlled, isolated resources where possible.
- [ ] Rehearse missed-slot catch-up with a disposable persistent timer on the
  actual host. CI already covers this behavior; preserve normal crawl scheduling.
- [ ] Rehearse a clean replacement host using an actual off-site backup,
  recovered private configuration/credentials and DNS recovery. Verify discovery,
  services, scheduling and alerts; measure data loss and total recovery time.
  Confirm free capacity before provisioning another host. Follow
  [the recovery runbook](deployment/OPERATIONS.md#complete-vm-loss-or-replacement).

## Needs observation time or a real event

- [ ] After the three-day window, check that newly published active memberships
  exclude repositories not rediscovered within retention. Older retained
  generations may still reference those repositories.
- [ ] After the seven-day window, verify retired/failed generation cleanup,
  cascading crawl/membership deletion and removal of unreferenced repository rows.
- [ ] Observe local backup pruning after seven days: a new verified off-site
  backup must precede pruning, and at least two verified local copies must remain.
- [ ] Measure several days of full-crawl runtime, memory/OOM counters, database
  and backup growth, disk/inode use and discovery latency. Review Gmail sending
  and OCI storage/network usage against the confirmed free allowances.
- [ ] Observe Caddy renew its certificate and serve the renewed certificate
  successfully. Initial issuance and the tested expiry alerts do not prove renewal.
- [ ] Confirm production catch-up after a genuinely missed six-hour slot during
  maintenance or an outage. The successful reboot did not miss a crawl slot.

## Parked by the operator

- [ ] Make a second independent offline recovery-key copy when ready. Another
  partition on the same physical disk does not provide independent redundancy.

## Optional, only if enabled

- [ ] Before using production pgAdmin, verify its real Bastion forwarding path
  and external port isolation. Follow [PGADMIN.md](deployment/PGADMIN.md); it is
  not part of the normal small-VM stack.
