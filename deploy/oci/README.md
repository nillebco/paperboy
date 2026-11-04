# Deploying Matins on OCI

Target: one free-tier VM, two hostnames (`matins.nilleb.co`, `matins.nilleb.com`)
on the same instance, TLS by Caddy, state on a Docker volume.

## 1. DNS

`dns-zones/` (in the vault, not in this repo) already carries the records, gated
on a variable so nothing is published until you supply the IP:

```hcl
# nilleb_co.tf / nilleb_com.tf
resource "cloudflare_record" "nilleb_co_matins" {
  count           = var.matins_ip == "" ? 0 : 1
  zone_id         = data.cloudflare_zone.nilleb_co.id
  name            = "matins"
  type            = "A"
  content         = var.matins_ip
  proxied         = false   # keep false for Caddy's automatic TLS
  allow_overwrite = true
}
```

```bash
cd ~/dev/nillebco/dns-zones
terraform apply -var="matins_ip=<PUBLIC_IP>"
```

Proxied stays `false` on purpose: with Cloudflare in front, Let's Encrypt's
HTTP-01 challenge never reaches Caddy. If you want the proxy (WAF, caching),
switch Caddy to DNS-01 with the Cloudflare plugin instead.

## 2. Firewall

Open 80 and 443 on the OCI VCN security list and on the instance's own iptables
(OCI images ship a REJECT rule by default):

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save    # or iptables-services / Oracle's persistence
```

## 3. The VM

```bash
sudo mkdir -p /srv/matins && sudo chown "$USER" /srv/matins
git clone git@github.com:nillebco/paperboy.git /srv/matins/app   # or a release tarball
cd /srv/matins/app/deploy/oci

cp env.example /srv/matins/env
chmod 600 /srv/matins/env
# Edit /srv/matins/env: ENCRYPTION_KEY, ADMIN_TOKEN, SMTP if wanted.
```

## 4. Build and start

```bash
cd /srv/matins/app
docker compose -f deploy/oci/docker-compose.yml up -d --build
docker compose -f deploy/oci/docker-compose.yml ps
curl -fsS https://matins.nilleb.co/healthz
```

First start takes a minute: the image builds the wheel from the hash-pinned
lockfiles, and Caddy issues the certificate on first request.

## 5. First account

1. Open `https://matins.nilleb.co/signin` and sign in with an email, or use
   `/start` for a catalogue with no email.
2. Under **Feeds**, paste a site or feed address, or import an OPML file. The first
   feed turns on the daily issue.
3. Under **Next issue**, review the selection and press **Compose this issue**.
4. Under **Settings**, set the schedule and limits, and copy the address from
   **Reader** into KOReader or CrossPoint.

## Operations

```bash
docker compose -f deploy/oci/docker-compose.yml logs -f matins
curl -fsS https://matins.nilleb.co/health            # config summary, no secrets
docker compose -f deploy/oci/docker-compose.yml exec matins \
  ls -la /data /data/issues                          # volumes and issues
```

Force an issue for one catalogue (bypasses the schedule and the "already built
today" check):

```bash
curl -fsS -X POST "https://matins.nilleb.co/admin/issues/run?secret=<secret>&force=1" \
  -H "X-Admin-Token: $ADMIN_TOKEN"
```

### Backup

`backup.sh` runs nightly from a systemd timer (02:30 UTC, `Persistent=true`):
it snapshots `app.db` through SQLite's online backup API inside the app
container (a tar of a live WAL database can tear), checks `integrity_check`,
then archives the snapshot plus the rest of the volume to
`/srv/matins/backups/matins-<UTC stamp>.tgz` (0600, owner `ubuntu`). The newest
7 are kept.

```bash
sudo cp deploy/oci/systemd/matins-backup.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now matins-backup.timer
sudo systemctl start matins-backup.service      # run one now
journalctl -u matins-backup.service -n 5
```

The box is a single point of failure, so a copy must leave it: a workstation
pulls the archives with `rsync` over SSH and checks the newest one opens
(`~/.local/bin/matins-backup-pull.sh` on the operator's Mac, kept 30 days, mirrored to Proton Drive). It alerts on Telegram when the pull, the integrity check or the Proton copy fails, or when the newest archive is over 36 h old — so a dead box timer is caught too.

`ENCRYPTION_KEY` is not in the backup — store it in the password manager, or
the backup restores into an app that cannot decrypt any stored token.

Restore: stop `matins`, extract the archive into the `matins_matins-data`
volume, then `mv .snapshot/app.db app.db && rm -rf .snapshot` and start it.

### Upgrade

```bash
cd /srv/matins/app && git pull && \
  docker compose -f deploy/oci/docker-compose.yml up -d --build
```

Upstream (`brendanlefebvre/later-ink`) stays as the `upstream` remote, so a
Later.Ink release can be merged with `git fetch upstream && git merge
upstream/main`. The Matins additions live in `src/later_ink/matins/` plus a
handful of hooks in `main.py`, which keeps those merges small.

## Notification email (Brevo)

SMTP is optional: with no `SMTP_HOST`/`SMTP_FROM`, `notify.py` returns False and
the webhook still fires. To enable it, add these to `/srv/matins/env` (the
password comes from the password manager, not the repo):

```
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USERNAME=b0ce0f001@smtp-brevo.com
SMTP_PASSWORD=<Brevo SMTP key>
SMTP_FROM=hello@nilleb.com
SMTP_TLS=starttls
```

Three things that cost an hour if you meet them cold:

- **`525 Unauthorized IP address`** means the SMTP key is IP-allowlisted and the
  instance is not on it. Authorise the instance's *egress* IPv4 (the public IP,
  because outbound traffic is SNAT'd through the internet gateway). This box has
  no global IPv6 and `smtp-relay.brevo.com` publishes A records only, so one
  address is the whole list. A Reserved public IP keeps that address stable if
  the instance is ever rebuilt.
- **DKIM on Brevo is two CNAMEs**, `brevo1._domainkey` and `brevo2._domainkey`,
  delegating to `b1/b2.<domain-with-dashes>.dkim.brevo.com`. There is no
  `mail._domainkey` TXT to look for — that was the old scheme, and hunting for
  it makes a working setup look broken.
- **No SPF change is needed** when DKIM is aligned: DMARC passes on DKIM, and
  Brevo's Return-Path is on its own domain, so the apex SPF is not in this path.
  Do not add a second `v=spf1` TXT — that is a permerror.

Verify credentials without composing an issue:

```bash
docker compose -f deploy/oci/docker-compose.yml exec -T matins python - <<'PY'
import asyncio, os, smtplib
from dataclasses import dataclass
from later_ink.matins import notify

s = smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), timeout=20)
s.ehlo(); s.starttls(); s.ehlo()
s.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
print("auth ok"); s.quit()

@dataclass
class Issue:
    id: str = "test"
    title: str = "Matins — test"
    article_count: int = 1
    word_count: int = 250

print("accepted" if asyncio.run(
    notify.send_issue_notification("you@example.com", Issue(), "https://matins.example/x/")
) else "failed")
PY
```

The message is deliberately a subject line and a link — no attachment, so there
is no Send-to-Kindle path and no size limit to maintain.

## Supply chain

Inherited from upstream and deliberately kept:

- Runtime dependencies are pinned **and hashed** (`requirements*.txt`), built
  with `pip --require-hashes`. CI installs from the lockfiles, not from
  `pyproject.toml` ranges.
- The base image in `Dockerfile` is pinned by **multi-arch index digest**;
  `.github/dependabot.yml` refreshes the digest weekly.
- The wheel is built in a first stage so the PEP 517 backend never ships.
- `pip-audit` runs against all four lockfiles in CI.

Matins adds **no runtime dependencies**. RSS parsing, extraction, scheduling,
HMAC signing and SMTP all use `lxml`/stdlib, which is the main reason the
feature set is shaped the way it is. Caddy is the one new image and is pinned by
index digest in `docker-compose.yml`; re-resolve it when you bump it:

```bash
docker buildx imagetools inspect caddy:2.10-alpine | grep Digest
```
