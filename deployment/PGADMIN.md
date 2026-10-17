# Optional private production pgAdmin

The optional `compose.pgadmin.yaml` overlay is not loaded by normal deployment.
It starts only the `admin` profile, joins the private database network, and binds
the web port to **127.0.0.1 only**. It has no Caddy route and does not publish
PostgreSQL. A separate admin access bridge permits host port publishing (Docker
does not publish from an internal-only network); it also permits outbound
traffic. No application service joins that bridge. Stop pgAdmin after use.
Do not add a public firewall rule,
public hostname, or `0.0.0.0` binding for it.

The pinned [official pgAdmin image](https://www.pgadmin.org/docs/pgadmin4/9.18/container_deployment.html)
runs as UID/GID 5050 on an unprivileged container port. Its settings/session
database persists in a dedicated volume; initialization config also requires a
writable container filesystem, unlike the read-only application image. CPU,
memory, PID and log limits are bounded. This is an administrative tool within
the trusted Docker network boundary, not a separate untrusted tenant.

## Prepare private credentials

From the matching verified release directory, copy `deployment/pgadmin.env.example`
to `/etc/gh-random/pgadmin.env`, owned by root with mode `0600`, and replace its
email. Create a unique random login password in a private editor at
`/etc/gh-random/pgadmin-password.txt`. Do not place it in an issue, command line,
shell history or Compose environment. Set the password file's owner/group to
`root:5050` and mode `0440`, so the non-root container can read the bind-mounted
file; keep its **host parent directory** root-owned mode `0700`. Numeric group
5050 should not grant an untrusted host user access. Docker administrators can
still read it. Never reuse the PostgreSQL password or mount other app secrets.

The file is referenced through PGADMIN_DEFAULT_PASSWORD_FILE. It initializes
the login only on first use of the pgAdmin data volume; editing it does not
rotate an existing account's password. Rotate that account through pgAdmin's
private authenticated interface or its documented account-management tooling.

## Start, tunnel, connect, stop

Use the same project name and production Compose file as the running database.
Replace the release path below with the verified deployed version. Start only
the admin service; do not recreate or upgrade the database as a side effect:

```sh
sudo docker compose --project-name gh-random --env-file /etc/gh-random/compose.env --env-file /etc/gh-random/pgadmin.env -f /opt/gh-random/releases/v0.1.0/compose.production.yaml -f /opt/gh-random/releases/v0.1.0/compose.pgadmin.yaml --profile admin up --detach --no-deps pgadmin
```

Establish a normal **managed SSH** session to the VM via OCI Bastion first.
On your laptop, add a loopback-only local forward to that session using its
documented generated ProxyCommand/SSH configuration, or, with an already working
`gh-random-vm` SSH host alias configured for that Bastion session:

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:5050:127.0.0.1:5050 gh-random-vm
```

This is SSH local forwarding **through a VM shell connection**, not an OCI
Bastion direct port-forwarding session to the VM's private-IP port 5050 (which
cannot reach a loopback-only bind). Do not replace the bind to make that latter
mode work. Bastion session expiry closes access; recreate the session/alias as
needed. If laptop port 5050 is occupied use 5051 on the left and browse that port.
Keep the SSH window open and visit `http://127.0.0.1:5050` locally. The remote hop
is encrypted by SSH; there is no public pgAdmin HTTP listener.

Log in with the pgAdmin email/password. Register a server with host `database`,
port `5432`, maintenance database matching `POSTGRES_DB`, and the intended
database user/password. `database` is Docker's service DNS name, not the VM's
public hostname. Prefer a restricted read-only database role for browsing;
administrative credentials allow destructive edits. Do not save production
database passwords unnecessarily. Browse Databases → your database → Schemas →
public → Tables. Schema changes still go through reviewed Alembic migrations,
not manual GUI edits; take a verified backup before intentional data edits.

When finished, stop and remove **only** the admin container and close the SSH
forward (Ctrl+C); retain its data volume unless deliberately resetting pgAdmin:

```sh
sudo docker compose --project-name gh-random --env-file /etc/gh-random/compose.env --env-file /etc/gh-random/pgadmin.env -f /opt/gh-random/releases/v0.1.0/compose.production.yaml -f /opt/gh-random/releases/v0.1.0/compose.pgadmin.yaml --profile admin rm --stop --force pgadmin
```

Do not run `down --volumes` against production. The pgAdmin volume contains
session/settings and possibly saved credentials; protect it with the same host
access boundary. It is not included in application PostgreSQL backups and is
not essential recovery data. Inspect host bindings and verify the public port
is inaccessible after actual OCI setup. CI only exercises a disposable stack.
