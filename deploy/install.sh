#!/usr/bin/env bash
# Blast Radius Zero — Ubuntu 24.04 (noble) installer.
#
# Idempotent: safe to re-run after editing code, after a failed gVisor install,
# or to upgrade. Run as root from a checkout:
#
#   sudo BRZ_DOMAIN=203.0.113.10.sslip.io ./deploy/install.sh
#
# What it does:
#   1. docker-ce (official repo) + python3-venv + Caddy (official repo)
#   2. gVisor runsc (official apt repo); on failure it warns loudly and keeps runc
#   3. /opt/brz app copy, venv, systemd unit, sandbox image build
#   4. Caddy site config, but only starts Caddy when BRZ_DOMAIN is set
#
# It never writes secrets: /opt/brz/.env is seeded from .env.example and must be
# edited by hand before the service can reach the model API.
set -Eeuo pipefail
IFS=$'\n\t'

APP_DIR="${APP_DIR:-/opt/brz}"
SERVICE_NAME="brz"
SERVICE_USER="brz"
SANDBOX_IMAGE="${SANDBOX_IMAGE:-brz-sandbox:1}"
BRZ_DOMAIN="${BRZ_DOMAIN:-}"
export DEBIAN_FRONTEND=noninteractive

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd -- "$here/.." && pwd)"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
_brz_err() { printf '\n\033[1;31mERROR: %s failed at line %s (exit %s)\033[0m\n' "${BASH_SOURCE[0]}" "$1" "$2" >&2; }
trap '_brz_err "$LINENO" "$?"' ERR

# ---------------------------------------------------------------- preconditions
[[ ${EUID} -eq 0 ]] || die "run as root (sudo $0)"
[[ -f "$root/requirements.txt" && -d "$root/app" ]] || die "run this from the project checkout ($root has no app/ or requirements.txt)"
if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || \
    warn "target platform is Ubuntu 24.04; detected ${PRETTY_NAME:-unknown}. Continuing, but packages may differ."
fi
command -v systemctl >/dev/null 2>&1 || die "systemd is required"

# ------------------------------------------------------------------ base system
log "installing base packages"
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl gnupg jq rsync \
  python3 python3-venv python3-pip \
  lsb-release apt-transport-https

# ------------------------------------------------------------------------ docker
if command -v docker >/dev/null 2>&1; then
  log "docker already installed ($(docker --version))"
else
  log "installing docker-ce from download.docker.com"
  install -m 0755 -d /etc/apt/keyrings
  if curl -fsSL --retry 3 https://download.docker.com/linux/ubuntu/gpg -o /tmp/docker.gpg.asc \
     && gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg /tmp/docker.gpg.asc; then
    chmod a+r /etc/apt/keyrings/docker.gpg
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu %s stable\n' \
      "$(dpkg --print-architecture)" "$(. /etc/os-release && echo "$VERSION_CODENAME")" \
      > /etc/apt/sources.list.d/docker.list
    if apt-get update -qq \
       && apt-get install -y -qq --no-install-recommends \
            docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; then
      :
    else
      warn "docker-ce install failed; falling back to the Ubuntu docker.io package"
      rm -f /etc/apt/sources.list.d/docker.list
      apt-get update -qq
      apt-get install -y -qq --no-install-recommends docker.io
    fi
  else
    warn "could not fetch the Docker apt key; falling back to docker.io"
    apt-get install -y -qq --no-install-recommends docker.io
  fi
fi

systemctl enable --now docker >/dev/null 2>&1 || true
docker info >/dev/null 2>&1 || die "docker daemon is not reachable after install"

# ------------------------------------------------------------------------ gvisor
configure_runsc_runtime() {
  python3 - <<'PY'
import json, pathlib, sys
path = pathlib.Path("/etc/docker/daemon.json")
cfg = {}
if path.exists() and path.read_text().strip():
    try:
        cfg = json.loads(path.read_text())
    except json.JSONDecodeError:
        print("daemon.json is not valid JSON; refusing to touch it", file=sys.stderr)
        sys.exit(1)
runtimes = cfg.setdefault("runtimes", {})
want = {"path": "/usr/bin/runsc"}
if runtimes.get("runsc") != want:
    runtimes["runsc"] = want
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    print("changed")
else:
    print("unchanged")
PY
}

install_gvisor() {
  local arch
  arch="$(dpkg --print-architecture)"
  curl -fsSL --retry 3 https://gvisor.dev/archive.key -o /tmp/gvisor.key
  gpg --dearmor --yes -o /usr/share/keyrings/gvisor-archive-keyring.gpg /tmp/gvisor.key
  chmod 0644 /usr/share/keyrings/gvisor-archive-keyring.gpg
  printf 'deb [arch=%s signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main\n' \
    "$arch" > /etc/apt/sources.list.d/gvisor.list
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends runsc
  runsc --version
}

GVISOR_OK=0
if command -v runsc >/dev/null 2>&1 && docker info --format '{{json .Runtimes}}' 2>/dev/null | jq -e 'has("runsc")' >/dev/null 2>&1; then
  log "gVisor runsc already registered with Docker ($(runsc --version 2>/dev/null | head -n1))"
  GVISOR_OK=1
else
  log "installing gVisor runsc from the official apt repo"
  if install_gvisor; then
    changed="$(configure_runsc_runtime)" || changed="failed"
    if [[ "$changed" == "failed" ]]; then
      warn "could not register runsc in /etc/docker/daemon.json"
    else
      [[ "$changed" == "changed" ]] && systemctl restart docker
      if docker info --format '{{json .Runtimes}}' | jq -e 'has("runsc")' >/dev/null 2>&1; then
        GVISOR_OK=1
        log "gVisor registered: $(runsc --version | head -n1)"
      fi
    fi
  fi
  if [[ $GVISOR_OK -ne 1 ]]; then
    warn "======================================================================"
    warn "gVisor (runsc) is NOT available. Falling back to the stock runc runtime."
    warn "Containers will share the host kernel: isolation is namespace + cgroup"
    warn "only, which is weaker against kernel exploits. The app stays functional"
    warn "(SANDBOX_RUNTIME=auto picks runc), but do not describe this host as"
    warn "'gVisor-hardened' until you fix this. Retry later with:"
    warn "    sudo BRZ_DOMAIN=<domain> $0"
    warn "======================================================================"
    # Leave apt in a working state even if the gVisor repo was the problem.
    rm -f /etc/apt/sources.list.d/gvisor.list
    apt-get update -qq || true
  fi
fi

# ------------------------------------------------------------------------- caddy
if ! command -v caddy >/dev/null 2>&1; then
  log "installing Caddy from the official apt repo"
  if curl -fsSL --retry 3 https://dl.cloudsmith.io/public/caddy/stable/gpg.key -o /tmp/caddy.key \
     && gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg /tmp/caddy.key; then
    chmod 0644 /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -fsSL --retry 3 https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends caddy
  else
    warn "Caddy repo setup failed; trying the Ubuntu caddy package (older, no auto-HTTPS guarantees)"
    apt-get install -y -qq --no-install-recommends caddy || \
      die "Caddy could not be installed; install it manually and re-run"
  fi
else
  log "caddy already installed ($(caddy version | head -n1))"
fi

# --------------------------------------------------------------------- app files
log "installing application into $APP_DIR"
install -d -m 0755 "$APP_DIR" "$APP_DIR/data"

if [[ "$root" != "$APP_DIR" ]]; then
  install -m 0644 "$root/requirements.txt" "$APP_DIR/requirements.txt"
  rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$root/app/" "$APP_DIR/app/"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' "$root/sandbox/" "$APP_DIR/sandbox/"
  rsync -a "$root/deploy/" "$APP_DIR/deploy/"
  install -m 0644 "$root/.env.example" "$APP_DIR/.env.example"
else
  log "running from $APP_DIR itself; skipping the self-copy"
fi
chmod 0755 "$APP_DIR/deploy/"*.sh

# ------------------------------------------------------------------- service user
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  log "creating system user $SERVICE_USER"
  useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# The service shells out to the docker CLI, so it must be in the socket's group.
if getent group docker >/dev/null; then
  usermod -aG docker "$SERVICE_USER"
else
  warn "no 'docker' group on this host: $SERVICE_USER cannot reach the Docker socket."
  warn "Fix with: sudo groupadd -r docker && sudo usermod -aG docker $SERVICE_USER && sudo systemctl restart docker"
fi
install -d -m 0775 -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR/data"

# -------------------------------------------------------------------------- venv
log "creating/refreshing the virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install --quiet --requirement "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/python" - <<'PY'
import fastapi, httpx, pydantic, uvicorn
print(f"venv ready: fastapi {fastapi.__version__}, pydantic {pydantic.VERSION}, uvicorn {uvicorn.__version__}")
PY

# ---------------------------------------------------------------------- env file
ENV_CREATED=0
if [[ ! -f "$APP_DIR/.env" ]]; then
  install -m 0600 "$APP_DIR/.env.example" "$APP_DIR/.env"
  ENV_CREATED=1
fi

# ------------------------------------------------------------------ systemd unit
log "installing systemd unit"
install -m 0644 "$APP_DIR/deploy/brz.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null

# ------------------------------------------------------------------ sandbox image
log "building the sandbox image"
SANDBOX_IMAGE="$SANDBOX_IMAGE" bash "$APP_DIR/deploy/build-sandbox.sh"

if [[ $GVISOR_OK -eq 1 ]]; then
  log "verifying runsc can actually start the sandbox image"
  if ! docker run --rm --runtime=runsc --network none --read-only --user 65534:65534 \
        "$SANDBOX_IMAGE" python -c 'print("runsc ok")'; then
    warn "runsc is installed but failed to start a container (common on nested/virtualised hosts)."
    warn "Set SANDBOX_RUNTIME=runc in $APP_DIR/.env to keep the service healthy."
  fi
fi

# ------------------------------------------------------------------------ caddy
install -d -m 0755 /etc/caddy
install -m 0644 "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
if [[ -n "$BRZ_DOMAIN" ]]; then
  log "configuring Caddy for $BRZ_DOMAIN"
  install -d -m 0755 /etc/systemd/system/caddy.service.d
  cat > /etc/systemd/system/caddy.service.d/10-brz-domain.conf <<EOF
[Service]
Environment=BRZ_DOMAIN=$BRZ_DOMAIN
EOF
  systemctl daemon-reload
  if BRZ_DOMAIN="$BRZ_DOMAIN" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    systemctl enable caddy >/dev/null
    systemctl restart caddy
  else
    warn "Caddyfile failed validation; Caddy left untouched. Check /etc/caddy/Caddyfile"
  fi
else
  warn "BRZ_DOMAIN is empty: Caddy config installed but not started."
  warn "Re-run with a domain once DNS/sslip.io is ready:"
  warn "    sudo BRZ_DOMAIN=203.0.113.10.sslip.io $0"
fi

# ------------------------------------------------------------------------ start
log "starting $SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

healthy=0
for _ in $(seq 1 30); do
  if curl -fsS -m 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then healthy=1; break; fi
  sleep 1
done

if [[ $healthy -eq 1 ]]; then
  log "service is up: $(curl -fsS http://127.0.0.1:8000/api/health)"
else
  warn "service did not answer on 127.0.0.1:8000 within 30s. Inspect:"
  warn "    systemctl status $SERVICE_NAME --no-pager"
  warn "    journalctl -u $SERVICE_NAME -n 80 --no-pager"
fi

# ------------------------------------------------------------------------ recap
if [[ $GVISOR_OK -eq 1 ]]; then
  RUNTIME_LINE="gVisor runsc (registered)"
else
  RUNTIME_LINE="runc — WEAKER ISOLATION, gVisor install failed"
fi

cat <<EOF

================ Blast Radius Zero installed ================
 app dir      : $APP_DIR
 service      : $SERVICE_NAME.service (127.0.0.1:8000)
 sandbox image: $SANDBOX_IMAGE
 runtime      : $RUNTIME_LINE
 caddy        : ${BRZ_DOMAIN:-<not configured — set BRZ_DOMAIN>}
=============================================================
Next steps:
 1. Edit $APP_DIR/.env and set INFERENCE_API_KEY (and MOCK_INFERENCE=0).
 2. sudo systemctl restart $SERVICE_NAME
 3. Open http://<public-ip>/ once Caddy reports a certificate.
EOF

if [[ $ENV_CREATED -eq 1 ]]; then
  warn "$APP_DIR/.env was created from .env.example with EMPTY secrets."
  warn "Edit it now: INFERENCE_API_KEY=... (mode 0600, root-owned)."
fi
