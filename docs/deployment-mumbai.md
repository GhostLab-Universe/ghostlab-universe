# Mumbai restricted-eval deployment

Single-host eval deployment in `ap-south-1`: one `t3.medium` (Ubuntu 24.04,
30 GB gp3) behind Caddy, loopback-only API, same-origin static UI, hourly
SQLite backups to a private versioned S3 bucket. Everything in this package is
new files under `deploy/` plus this document; it consumes the API/security
auth seam read-only and duplicates none of it.

## 0. Hard dependency: hosted auth waits for PR #22

Hosted auth cutover waits for **PR #22 merge** (OPEN at the time of writing).
Until then:

- `local_dev` is refused off-loopback by `src/ghostlab/security/local_dev.py`
  (`require_local_dev` raises unless the bind host is loopback, and refuses
  outright when `GHOSTLAB_DEPLOYMENT != "local"`). There is **no
  local-dev-behind-proxy mode**: putting `local_dev` behind Caddy does not
  authenticate anybody and is not a supported configuration.
- The API unit (`deploy/systemd/ghostlab-api.service`) takes
  `GHOSTLAB_AUTH_MODE` from the operator-owned `/etc/ghostlab/env/api.env`
  and consumes whatever PR #22 lands, unchanged.
- Pre-merge, NOTHING behind Caddy runs the eval: the pinned
  `GHOSTLAB_DEPLOYMENT=hosted` example fails closed because the sole
  pre-merge session path is `require_local_dev` over loopback. The only
  operator step before PR #22 is a TEMPORARY loopback run over an SSH
  forward (`GHOSTLAB_DEPLOYMENT` unset, `GHOSTLAB_AUTH_MODE=local_dev`,
  API on 127.0.0.1, Caddy stopped or never routing to it), torn down
  afterwards. Do not open the eval to anyone who is not an operator
  until PR #22's auth mode is configured.

## 1. Architecture

```text
evaluators ──443──▶ Caddy ──┬── /v1/* ──▶ 127.0.0.1:8787 ghostlab-api.service
 (domain mode)      (TLS:        │
  Let's Encrypt)    └── * ──▶ static /srv/ghostlab/web/dist (same origin)

IP-only mode: Caddy binds 127.0.0.1:8080 only; operator forwards 8080 over SSH.
```

| Layer | Choice |
| --- | --- |
| Region / AZ | `ap-south-1`, single public subnet in `ap-south-1a` |
| Host | `t3.medium`, Ubuntu 24.04 (Canonical `099720109477` image), IMDSv2 required |
| Disk | 30 GB gp3 root, encrypted; storage root `/var/lib/ghostlab` on it |
| Address | Elastic IP; Route53 A record only when `domain_name` is set |
| Ingress | 443 open; 80 open for ACME http-01 only; 22 only from `admin_cidr` (unset = no rule at all); no DB/worker ports anywhere |
| TLS | Caddy automatic Let's Encrypt when a domain is set |
| API | `python -m ghostlab.api`, `--host 127.0.0.1` hardcoded in `run-api.sh` |
| UI | Production `vite build` output served statically by Caddy, same origin |
| Live model | `muse-spark-1.3` only, via the existing `--live` gate; key by env, never in Git |

File map:

- `deploy/terraform/` — VPC, subnet, SG, host, EIP, S3, Route53, user-data.
- `deploy/caddy/` — `Caddyfile.domain` (public TLS) and `Caddyfile.localhost` (IP-only).
- `deploy/systemd/` — `ghostlab-api.service`, `ghostlab-backup.service/.timer`, `api.env.example` (empty).
- `deploy/scripts/` — `install-host.sh` (operator installer), `run-api.sh` (flag wrapper).
- `deploy/backup/` — `ghostlab-backup.sh`, `ghostlab-restore.sh`, `restore-drill.sh`.
- `deploy/config.mumbai.json.example` — filled-in-by-operator example, no secrets.
- `deploy/tests/` — config schema check + backup round-trip proof.

## 2. Provision

Prerequisites (see §8 blockers): AWS access with rights for EC2/VPC/S3/Route53/IAM,
a domain name (or explicit IP-only approval), an EC2 key pair, spend approval.

```bash
cd deploy/terraform
terraform init
terraform plan \
  -var='domain_name=eval.example.org' \
  -var='acme_email=ops@example.org' \
  -var='admin_cidr=203.0.113.7/32' \
  -var='key_name=ops-keypair'
```

Review the plan, then the exact apply command the operator runs:

```bash
terraform apply \
  -var='domain_name=eval.example.org' \
  -var='acme_email=ops@example.org' \
  -var='admin_cidr=203.0.113.7/32' \
  -var='key_name=ops-keypair'
```

For IP-only mode, drop `domain_name`/`acme_email` (empty = IP-only: no zone,
no records, localhost-only proxy) but keep `admin_cidr` and `key_name` so the
box stays reachable. If the zone already exists, add
`-var='route53_zone_id=Z...'`.

After apply, record the outputs:

```bash
terraform output public_ip backup_bucket address
```

## 3. Install the app

SSH in over the fenced CIDR (`ssh ubuntu@<public_ip>`), then from a pinned
commit:

```bash
sudo mkdir -p /srv/ghostlab && sudo chown ubuntu:ubuntu /srv/ghostlab
git clone <repo-url> /srv/ghostlab/app
cd /srv/ghostlab/app
git checkout <pinned-commit>
sudo deploy/scripts/install-host.sh
```

`install-host.sh` builds the locked venv (Python 3.13 via uv), builds the
production web bundle, installs the Caddyfile matching the domain/IP-only
mode, validates it (`caddy validate`), installs the systemd units and backup
scripts, enables the backup timer, and prepares `/var/lib/ghostlab`. It
writes no secrets.

Then create the operator-owned env file (the ONLY place keys and live flags
live) and start the API:

```bash
sudo cp deploy/systemd/api.env.example /etc/ghostlab/env/api.env
sudo chmod 0600 /etc/ghostlab/env/api.env
sudo chown ghostlab:ghostlab /etc/ghostlab/env/api.env
sudoedit /etc/ghostlab/env/api.env   # fill in; never commit or print
sudo systemctl start ghostlab-api.service
sudo systemctl status ghostlab-api.service
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Live mode is the existing `--live` gate: set `GHOSTLAB_LIVE=1`,
`GHOSTLAB_LIVE_MODEL=muse-spark-1.3`, `GHOSTLAB_LIVE_MAX_CALLS`,
`GHOSTLAB_LIVE_COMPAT_REPORT` (passing M01 report), and
`GHOSTLAB_CONFIRM_PAID_CALLS=1`, with `MODEL_API_KEY` in the same file. The
wrapper passes the model through; `live.py` refuses anything but the
qualified id. No other model id or endpoint is valid; do not invent one.

Placement: the unit runs under `ProtectSystem=strict`/`ProtectHome`, so
`GHOSTLAB_LIVE_COMPAT_REPORT` must live under `/srv/ghostlab/app` or
`/var/lib/ghostlab` -- never under `/home` or `/root`, which the unit
cannot read.

## 4. IP-only mode

With `domain_name` empty, Caddy binds `127.0.0.1:8080` only and the security
group still exposes nothing usable (443/80 have no public listener behind
them). Reach the eval over fenced SSH:

```bash
ssh -L 8080:127.0.0.1:8080 ubuntu@<public_ip>
# open http://127.0.0.1:8080 on the workstation
```

Cut over to a domain later by re-applying Terraform with `domain_name` set
and re-running `install-host.sh` (it swaps in `Caddyfile.domain`).

## 5. SQLite-safe DB plan

The storage root is `/var/lib/ghostlab` on the encrypted 30 GB gp3 volume,
laid out exactly as `StoragePaths` defines (`ghostlab.sqlite`,
`graph-checkpoints.sqlite`, `artifacts/`). One server per root (the API
takes a root lock at startup); no second writer, no network database.

Backups (`ghostlab-backup.timer`, hourly, `Persistent=true`):

1. `sqlite3 <live> ".backup main <stage>"` for each database — the online
   backup API, safe against a running server. Never raw-copy live `.sqlite`
   files (WAL side files).
2. `cp -a` of `artifacts/`.
3. `sha256sum` manifest over the snapshot.
4. `aws s3 sync` to `s3://<backup_bucket>/<UTC stamp>/`.
5. Local stage older than 48 h is pruned; S3 lifecycle expires prefixes and
   non-current versions after 30 days (see `backup_retention_days`).

Restore (`deploy/backup/ghostlab-restore.sh latest /var/lib/ghostlab`):

1. Sync the prefix locally, verify `sha256sum -c MANIFEST.sha256` FIRST.
2. Restore into a fresh directory, `PRAGMA integrity_check` on both DBs.
3. Refuse while `ghostlab-api.service` is running; stop it first.
4. Swap the directory into place (the old root is kept as
   `.pre-restore-<stamp>`), fix ownership, start the API.

Drill: run `deploy/backup/restore-drill.sh` on the host (or anywhere with
sqlite3 — no AWS needed) after every storage/migration change and monthly
regardless. It builds a scratch store, snapshots it with the same method,
destroys the original, restores, and requires byte-identical dumps plus
`integrity_check=ok`, exiting `DRILL: PASS`. The same round-trip is proven
in CI-able form by `deploy/tests/test_backup_roundtrip.py`.

Retention policy: hourly snapshots, 30-day S3 expiry for current and
non-current versions, 48-hour local stage. Backups are as private as the
store (memory records and approvals are inside them).

## 6. Rollback procedure

Two axes: machine image and app commit. Do them in this order.

**A. Snapshot before every change.** Before any app upgrade or Terraform
change:

```bash
aws ec2 create-snapshot --volume-id <root-volume-id> \
  --description "pre-<new-commit-or-change>-$(date -u +%Y%m%dT%H%MZ)" --region ap-south-1
aws s3 sync s3://<backup_bucket>/ s3://<backup_bucket>/pinned/pre-<change>/ --region ap-south-1
```

Keep the previous AMI id (from `terraform show` / the `data.aws_ami` pin in
state) and the previous app commit written next to the change ticket.

**B. App rollback (previous commit redeploy).** Failures after an app upgrade
are fixed by redeploying the last good commit; no Terraform change needed:

```bash
cd /srv/ghostlab/app
sudo systemctl stop ghostlab-api.service
git checkout <previous-good-commit>
sudo deploy/scripts/install-host.sh
sudo systemctl start ghostlab-api.service
```

If the failed upgrade also migrated the store forward, code rollback alone
does not work: restore the pre-change backup first
(`ghostlab-restore.sh <pre-change-stamp> /var/lib/ghostlab`), which is why
step A pins one. Migration notes live with the release checklist.

**C. Machine rollback (prior AMI/snapshot).** If the host itself is bad
(broken user-data, bad Terraform change):

1. In `deploy/terraform`, `terraform plan` with the previous variable set and
   confirm the diff only touches what you intend; or, for a full rebuild,
   `terraform destroy` the eval stack (the S3 bucket is `force_destroy=false`
   and survives) and re-`apply` the last good configuration.
2. Alternatively, launch a replacement from the pre-change snapshot: create a
   volume from the snapshot, attach it to a fresh instance from the prior
   AMI, re-associate the Elastic IP (its allocation survives in state).
3. Re-run §3 from the previous good app commit and restore the pinned backup.

**D. Rollback of the proxy.** The Caddyfile is validated before reload
(`install-host.sh` runs `caddy validate`), so a bad config never goes live.
If behaviour regresses anyway, re-run `install-host.sh` from the previous
good commit, which restores that commit's Caddyfile.

Verify after any rollback: `systemctl is-active ghostlab-api`,
`caddy validate`, the restore-drill `DRILL: PASS`, and one scripted campaign
round-trip before handing back to evaluators.

## 7. Validation (no cloud required)

What the package author ran (see commit message for SHAs):

- `PYTHONPATH=src python -m pytest deploy/tests/ -q` — config-example schema
  check pinned to the repo seams + backup/restore round-trip on scratch
  SQLite files. Must be green.
- `deploy/backup/restore-drill.sh` — shell twin of the round-trip; must print
  `DRILL: PASS`. Needs only sqlite3 + sha256sum.
- `terraform fmt -check` + `terraform validate` when Terraform is installed;
  this environment had no Terraform, so the `.tf` files got a brace/paren
  smoke scan instead (stated, not a substitute for `validate`).
- `grep` proof over `deploy/`: no secret values, no credentials, and the only
  model id present is the qualified `muse-spark-1.3` (see commit notes for
  the exact patterns).

Re-run all four before every apply.

## 8. Cost estimate and open blockers

Monthly estimate for 24/7 on-demand in `ap-south-1` (public list rates;
confirm in the AWS Pricing Calculator before spend approval):

| Item | Basis | ≈ USD/mo |
| --- | --- | --- |
| `t3.medium` Linux | ~$0.0416/h × 730 h | ~$30.40 |
| 30 GB gp3 root | ~$0.08–0.10/GB-mo | ~$2.50–3.00 |
| Elastic IP | free while attached | $0.00 |
| S3 backups | a few GB, versioned | < $1.00 |
| Route53 zone | $0.50/zone, domain mode only | $0.00–0.50 |
| **Total** | | **≈ $34–35** |

Open blockers:

1. **IdP choice** — which identity provider fronts the eval after PR #22
   merges; cutover config cannot be written until this is decided.
2. **Domain name** — the public eval hostname (or written approval for
   IP-only + SSH forwarding for the whole eval window).
3. **Spend approval** — ~$35/mo run-rate plus negligible backup storage.
4. **AWS access** — account, region guardrails for `ap-south-1`, and an IAM
   principal for the operator able to manage EC2/VPC/S3/Route53/IAM.
5. **PR #22 merge** — hosted auth itself; see §0. No public eval before it.
