# Remaining work

Reviewed 2026-10-30. This file contains unfinished operational work only.
Product enhancements belong in [ROADMAP.md](ROADMAP.md); completed production
checks are recorded in the [operations guide](../operations/OPERATIONS.md#live-acceptance--2026-10-28).

## Can do now

Access and free-capacity prerequisites are noted below; these tasks do not need
the catalog to age.

### Replacement-host recovery (private material, OCI and SSH access)

- [ ] Rehearse a clean replacement host using an actual off-site backup,
  recovered private configuration/credentials and DNS recovery. Verify discovery,
  services, scheduling and alerts; measure data loss and total recovery time.
  Recover private configuration independently of the surviving VM. Before
  provisioning, confirm actual free host capacity and remaining storage including
  retained volume backups; a service-limit allowance is not a capacity guarantee.
  Keep the account free and follow
  [the recovery runbook](../operations/OPERATIONS.md#complete-vm-loss-or-replacement).

## Needs observation time or a real event

- [ ] If regional connection timeouts recur, capture contemporaneous network
  evidence from the affected monitoring locations. The October 29 incident
  recovered automatically after 5m 8s; host/application logs showed no crash,
  restart or OOM and other clients received 200 responses during the window.
  The underlying reachability failure remains unexplained.
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
  and external port isolation. Follow [PGADMIN.md](../operations/PGADMIN.md); it is
  not part of the normal small-VM stack.
