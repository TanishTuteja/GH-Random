# Encrypted backups and recovery

This tooling is implemented and rehearsed with disposable PostgreSQL and real
age encryption. It does not create OCI resources, grant IAM permissions, enable
timers, generate production recovery keys, or certify live off-site recovery.
Those remain deployment acceptance tasks for the operator.

## Policy and trust

- Daily at **05:15 UTC / 10:45 IST**, between the 00:00 and 06:00 crawler slots.
  A persistent timer catches up once after downtime. A busy crawl/deployment
  refuses overlap; it does not disable the next day's backup.
- Initial recovery-point objective: **24 hours** of catalog data. Failures can
  exceed this, hence immediate email and a 36-hour missing-success alarm.
- Initial restore target: **under one hour after a replacement host is ready**.
  This is a target, not a measured production guarantee. Rehearse with the actual
  catalog and include separate time for VM provisioning/DNS/secrets recovery.
- Local retention: seven days, **at least two verified encrypted copies** even
  when old. Prune only after a new encrypted backup has been uploaded and fully
  downloaded again with a matching SHA-256. Unknown, corrupt and never-uploaded
  copies are not automatically removed. Email failures and inspect disk space.
- No automatic remote deletion. Use a dedicated private bucket/prefix, scoped
  create/read permissions and no overwrite/delete permissions for the VM. Plan
  quotas/retention during OCI setup. **Do not install an age-only lifecycle rule
  that can expire every recovery copy during a long backup outage.** Use an
  independently supervised retention process or preserve a protected recovery
  tier before enabling remote expiration. Test any retention rule explicitly.
- A backup unit is limited to 40 minutes plus a 90-second shutdown budget. Each
  transfer/encryption command defaults to 900 seconds; uploads default to a
  512 MiB ceiling (configurable up to 2 GiB). Review these against measured data
  size/free disk/OCI limits; limits are not capacity or completion guarantees.

Public age X25519 recipients alone live on the VM. Keep **two independent offline
copies** of the private recovery identity and an inventory of which recipient
protects each backup. Keep the private identity out of Git, the release image,
GitHub Actions secrets and normal VM configuration. Lost identities mean lost
backups. Rotate by encrypting to old and new public recipients until old archives
expire or have been safely re-encrypted/tested. The script rejects private keys,
SSH keys and plugin recipients in the public-recipient file.

Encryption uses official age, not custom cryptography. Ciphertext authentication
detects corruption, but **does not identify the sender**: someone with a public
recipient can create a different archive. Trust the source/storage account and
verify the expected checksum; `--trust-backup` is an explicit recovery decision.

## Install and configure (manual VM work)

From a trusted, verified matching release on the VM:

```sh
sudo python3 deployment/install_age.py --destination /usr/local/lib/gh-random/age-tools
sudo install -Dm0644 deployment/crawler.py /usr/local/lib/gh-random/crawler.py
sudo install -Dm0644 deployment/backup.py /usr/local/lib/gh-random/backup.py
sudo install -d -m0700 /var/backups/gh-random
```

The installer pins age 1.3.2's official archive checksums, extracts only age,
age-keygen and LICENSE, and refuses to overwrite different files. It supports
Linux ARM64/AMD64 and Windows AMD64. Review upstream security fixes and update
the pinned hashes in a PR; reinstall changed versions deliberately. Do not use
a symlink for the configured executable path. See [age upstream](https://github.com/FiloSottile/age).

On your **offline recovery machine**, use `age-keygen --output recovery-key.txt`
and derive the public value using `age-keygen -y recovery-key.txt`. Protect and
back up the identity first; copy only the public recipient line to root-owned
0600 **/etc/gh-random/backup-recipients.txt** on the VM. The installer does not
create or transmit production keys.

Copy backup.json.example to root-owned 0600 **/etc/gh-random/backup.json**. All
paths must be absolute. Set namespace, private bucket and dedicated object prefix.
Install Oracle's official OCI CLI at the configured absolute executable path;
use a supported, reviewed version and maintain it with host tooling updates.
The worker explicitly uses **instance_principal**, not an administrator's API
key/config. Configure its dynamic group/IAM policy in OCI; none is created here.
It needs upload and read-back access, not list/delete/overwrite across unrelated
objects. Configure Standard storage within your confirmed free quotas.

Uploads use `--no-overwrite --no-multipart --verify-checksum`; a unique name avoids
collisions. Then a version-constrained GET is downloaded and SHA-256 compared.
A bare CLI exit status, an existing object, or an upload without read-back does
not count as a successful off-site backup. See [OCI put](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/os/object/put.html)
and [get](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/os/object/get.html).
Actual IAM/transport/storage behavior must be verified after OCI setup.

Install/test email first, then the matching backup service/timer:

```sh
sudo install -Dm0644 deployment/systemd/gh-random-backup.service /etc/systemd/system/gh-random-backup.service
sudo install -Dm0644 deployment/systemd/gh-random-backup.timer /etc/systemd/system/gh-random-backup.timer
sudo systemd-analyze verify /etc/systemd/system/gh-random-backup.service /etc/systemd/system/gh-random-backup.timer
sudo systemctl daemon-reload
sudo systemctl start gh-random-backup.service
sudo journalctl -u gh-random-backup.service --since today
sudo systemctl enable --now gh-random-backup.timer
```

Set `monitor_backups` to true in private alerts.json **after** configuring/running
the backup worker. Verify failure/recovery and missing-success inbox delivery.
The alert template handles timeout/preflight failures even when a worker cannot
write backup.json. Normal success writes backup-success.json only after upload,
read-back, retention and catalog lease cleanup. A later failure does not advance
that watermark. Enable the timer, not the service at boot; stopping the timer
alone does not stop an active job. Stop both for urgent maintenance.

The installed backup bootstrap uses the current verified release's hash-checked
worker/digest, not moving main. Reinstall **both crawler.py and backup.py** from
the matching release when updating their shared dispatch protocol. The worker
holds the host deployment mutex and a fenced database coordinator lease; manual
crawls/migrations cannot overlap its protected snapshot. SQL statement timeouts
remain separate from the bounded coordinator idle period. Browsing stays live.
Docker access is root-equivalent despite unit sandboxing.

## Encrypt pre-deploy snapshots too

Pass both `--backup-config /etc/gh-random/backup.json` and the existing
`--alerts-config /etc/gh-random/alerts.json` to production deploy/rollback.
`--backup-dir` must equal the policy's archive_dir. Policy/tool/recipient checks
run before mutation. The script seals/uploads/read-back-verifies the snapshot
before DDL and refuses to continue if that fails. Without --backup-config the
legacy private **plaintext** backup remains available for explicit local use;
it is not the recommended production command and is not automatically pruned.

Raw dumps and an unencrypted tar exist **temporarily** in a private .work-* tree
while sealing/decrypting. Normal completion/failure removes only that exact tree;
this is not guaranteed forensic secure erasure on SSDs. SIGKILL/power loss can
leave private temporary files. After proving no backup/deploy owner is live,
inspect and remove only exact owned .work-* paths; never blanket-prune a backup
root. Underlying VM disk encryption and off-site age encryption address different
threats. Keep adequate space for raw dump, tar and ciphertext simultaneously.

## Recover without the old VM

Download the selected encrypted object using a separate recovery identity with
read-only bucket access. Record its expected ciphertext checksum from your
trusted receipt/inventory; restrict the downloaded file to mode 0600. Obtain a
trusted release's backup_archive.py and database_backup.py plus age. On the
recovery machine, decrypt to a **new private directory**:

```sh
python3 deployment/backup_archive.py --archive /private/recovery/database.tar.age \
  --identity /offline/recovery-key.txt --output-dir /private/recovery/unsealed \
  --age /private/tools/age --trust-backup
```

The command prints the recovered gh-random-* directory. It refuses tampering,
wrong keys, links, extra members, traversal, oversized content and overwrites.
The inner dump manifest/checksum is verified before returning. It never connects
to or changes a database. Keep plaintext recovery files private.

On an isolated/new PostgreSQL installation of the same major version, use the
existing safe restore tool with that directory and a **new** target database:

```sh
python3 deployment/database_backup.py --env-file /etc/gh-random/compose.env \
  --compose-file /opt/gh-random/releases/v0.1.1/compose.production.yaml \
  restore --backup /private/recovery/unsealed/gh-random-TIMESTAMP-ID \
  --target-database gh_random_restore_rehearsal --trust-backup
```

Never overwrite/drop the live database. Verify Alembic revision, catalog/run
counts and page/API discovery against the recovered database before any explicit
cutover. Restore GitHub App/SMTP credentials and DNS/host policy from their
separate recovery records; database archives intentionally do not contain them.
Do not leave the offline private identity on the normal production VM afterwards.

Local and native CI rehearse the actual encryption, restore into a separate
PostgreSQL database, and restored page/API discovery. Native systemd CI also runs
a failing backup timer followed by a normal successful slot using a **local fake
object store**, validates failure/recovery email, and decrypts copied-back bytes
with the private identity hidden from the backup unit. That proves the code path,
not OCI credentials, service limits, bucket immutability or real VM-loss recovery.
Perform and time the live off-site/VM-loss rehearsal before relying on this plan.
