#!/usr/bin/env bash
# Why the demo cannot be redeployed from this host, in one run.
#
# Run this before attempting a deploy: it distinguishes "the instance is down"
# from "the instance is fine but this host cannot reach a management channel",
# which are very different situations and lead to different fixes.
set -uo pipefail

IP="${1:-158.247.240.81}"
DOMAIN="${IP}.sslip.io"

say() { printf '\n== %s\n' "$1"; }

say "application is serving"
curl -sS -o /dev/null -w '   /api/health  HTTP %{http_code} in %{time_total}s\n' \
  --max-time 20 "https://${DOMAIN}/api/health"

say "which code is actually deployed"
# A session-scoped ledger returns [] to a cookie-less caller. Anything else means
# the deployed build predates the scoping change.
curl -sS --max-time 20 "https://${DOMAIN}/api/runs" 2>/dev/null \
  | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("   could not parse /api/runs"); raise SystemExit
print("   /api/runs with no cookie returned %d row(s)" % len(d))
print("   scoping deployed" if d == [] else "   scoping NOT deployed (old build)")'

say "sshd (port 22: TCP can open while the daemon never speaks)"
banner=$(timeout 20 bash -c "exec 3<>/dev/tcp/${IP}/22; timeout 15 head -c 40 <&3" 2>/dev/null)
if [ -n "$banner" ]; then
  echo "   banner: ${banner}"
else
  echo "   TCP opened but no SSH banner -> sshd is not accepting sessions"
fi

say "caddy (port 443: proves the instance itself is alive)"
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "https://${DOMAIN}/api/health" 2>/dev/null)
echo "   https://${DOMAIN}/api/health -> HTTP ${code:-000}"

say "vultr control API"
key_file="$HOME/.config/brz/vultr-api-key"
if [ -r "$key_file" ]; then
  curl -sS --max-time 20 -H "Authorization: Bearer $(<"$key_file")" \
    https://api.vultr.com/v2/account 2>/dev/null | head -c 300
  echo
else
  echo "   no API key at ${key_file}"
fi

say "vultr console in a browser (Cloudflare)"
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 https://console.vultr.com/ 2>/dev/null)
echo "   console.vultr.com -> HTTP ${code:-000}"

cat <<'EOF'

Reading the result:
  - health 200 + console 403/401  -> the instance is fine; only the management
    channels are blocked. Fix the firewall rule for 22 (Vultr Firewall Group
    "brz-demo" currently allows 22 from the deploy-time IP only) or open the
    instance console from the Vultr web UI.
  - no banner on 22               -> sshd itself is not accepting sessions.
    The web console is then the only way in: systemctl restart ssh, then deploy.
  - "scoping NOT deployed"        -> the running build predates commits
    05529a3/c1e57ee; redeploy is required for the fix to be live.
EOF