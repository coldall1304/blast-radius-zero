# Deploying Blast Radius Zero on Vultr

One page, from a bare Ubuntu 24.04 instance to a served instance. The app runs
as `brz` out of `/opt/brz` under systemd, binds **127.0.0.1:8000 only**, and is
published by Caddy on 80/443. Every agent step executes in a throwaway container
(`--network none --read-only --user 65534 --cap-drop ALL`, gVisor `runsc` when
available).

## 1. Instance

| Item | Recommendation |
|---|---|
| Plan | 2 vCPU / 4 GB RAM / 80 GB NVMe (minimum). 1 vCPU / 2 GB works for a demo but pandas steps get slow and OOM-prone. |
| OS | Ubuntu 24.04 LTS x64 |
| Region | Any; closest to the model API you use |
| Disk | The image pulls ~450 MB; artifacts accumulate under `/opt/brz/data/artifacts` |

**Firewall / Vultr Firewall Group:** allow inbound **22 (your IP only), 80, 443**.
Do **not** open 8000 — it is bound to loopback and Caddy is the only entry point.

## 2. Install

```bash
# on the instance
sudo apt-get update && sudo apt-get install -y git
git clone <your-repo> brz && cd brz

# <ip> is the instance's public IPv4; sslip.io resolves it, so no DNS needed.
sudo BRZ_DOMAIN="$(curl -s https://api.ipify.org).sslip.io" ./deploy/install.sh
```

`install.sh` is idempotent — re-run it after `git pull`, or to retry the gVisor
install. It installs docker-ce, python3-venv, Caddy, gVisor `runsc`, copies the
app to `/opt/brz`, builds `.venv`, installs and enables `brz.service`, and builds
the `brz-sandbox:1` image. It writes **no secrets**.

If the gVisor repo or `runsc` fails, the script prints a boxed warning, leaves
`SANDBOX_RUNTIME=auto` (→ runc) and continues. The site works; the containment
story is weaker. Fix it later by re-running the same command.

## 3. Configure secrets

```bash
sudoedit /opt/brz/.env       # mode 0600, root-owned
```

- `INFERENCE_API_KEY=` — Vultr Serverless Inference key.
- `MOCK_INFERENCE=1` is the shipped default so a fresh box cannot burn tokens.
  Set `MOCK_INFERENCE=0` once the key is in place.
- Leave `SANDBOX_NETWORK=none` and `SANDBOX_RUNTIME=runsc` (strict) or `auto`.

```bash
sudo systemctl restart brz
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/config | jq '{runtime, gvisor, image_present}'
```

## 4. Verify gVisor is actually in use

```bash
# registered with the daemon?
docker info --format '{{json .Runtimes}}' | jq 'has("runsc")'

# does a container really start under it?
docker run --rm --runtime=runsc --network none --read-only --user 65534:65534 \
  brz-sandbox:1 python -c 'print("runsc ok")'

# what the app reports to the browser
curl -s http://127.0.0.1:8000/api/config | jq '{runtime, gvisor}'
# expect: {"runtime":"runsc","gvisor":true}
```

`runtime: "runc"` means the fallback path is active: kernel exploits are no
longer absorbed by the user-space Sentry. Treat that as a real regression.

Useful dmesg line after a gVisor install: `runsc: Starting...`; run
`sudo runsc --version` to confirm the binary.

## 5. Operate

```bash
systemctl status brz --no-pager
journalctl -u brz -f                 # app logs
journalctl -u caddy -f               # TLS + access logs
docker ps --filter label=brz.run     # live sandbox containers (should drain to 0)
rm -rf /opt/brz/data/artifacts/*     # reclaim artifact disk
```

TLS: Caddy obtains and renews certificates automatically for `{$BRZ_DOMAIN}`
(via `caddy.service.d/10-brz-domain.conf`). Ports 80 and 443 must both be open —
80 is used for the ACME HTTP challenge and the HTTPS redirect.

SSE: `deploy/Caddyfile` sets `flush_interval -1` on `/api/runs/*/events` and
leaves that path uncompressed. If live logs stall, suspect a proxy in front of
Caddy (Cloudflare buffering, etc.) before suspecting the app.

## 6. Rollback

```bash
# stop serving but keep data
sudo systemctl disable --now brz caddy

# previous code: re-run the installer from the older checkout
git -C ~/brz checkout <previous-tag>
sudo BRZ_DOMAIN=... ~/brz/deploy/install.sh

# or restore the whole host
/opt/brz/data/brz.sqlite3           # only stateful file (plus artifacts/)
```

A rebuild of the sandbox image is safe to do independently:
`sudo /opt/brz/deploy/build-sandbox.sh` — running containers are unaffected
because each step gets a fresh container. If a bad image slips through, restore
the previous tag with `docker tag brz-sandbox:previous brz-sandbox:1`.

Uninstall: `systemctl disable --now brz caddy`, then `rm -rf /opt/brz` and
`docker rmi brz-sandbox:1`.

## 7. Files

| Path | Purpose |
|---|---|
| `sandbox/Dockerfile` | sandbox image (`python:3.12-slim` + numpy/pandas/matplotlib) |
| `deploy/build-sandbox.sh` | build + `--network none --read-only` smoke test |
| `deploy/install.sh` | idempotent Ubuntu 24.04 installer |
| `deploy/brz.service` | systemd unit for uvicorn on 127.0.0.1:8000 |
| `deploy/Caddyfile` | TLS reverse proxy, SSE-safe |
| `.env.example` | every knob, secure defaults, empty secrets |
| `requirements.txt` | pinned app dependencies |
