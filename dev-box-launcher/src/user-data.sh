#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/user-data.log) 2>&1

# ---- Set these ----
ALLOC_ID="eipalloc-0123456789abcdef0"
PROD_API_SERVER="https://d7i5c62g7weaq.cloudfront.net"
MODEL_ID="us.anthropic.claude-sonnet-4-6"

# ---- Instance details from IMDSv2 ----
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token \
-H "X-aws-ec2-metadata-token-ttl-seconds: 60")
meta() { curl -s -H "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/meta-data/$1"; }
INSTANCE_ID=$(meta instance-id)
REGION=$(meta placement/region)

# ---- Attach the Elastic IP and work out the hostname ----
aws ec2 associate-address --region "$REGION" \
--instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID" --allow-reassociation
EIP=$(aws ec2 describe-addresses --region "$REGION" --allocation-ids "$ALLOC_ID" \
--query 'Addresses[0].PublicIp' --output text)
HOST="${EIP//./-}.sslip.io"

# Wait (up to ~60s) until the instance actually egresses through that address
for _ in $(seq 1 30); do
[ "$(curl -s --max-time 3 https://checkip.amazonaws.com || true)" = "$EIP" ] && break
sleep 2
done

# ---- dev-box.env (read by the dev servers and the dev-box server) ----
cat > /home/ec2-user/.config/dev-box.env <<EOF
CODING_AGENT_ORIGINS=https://$HOST
CODING_AGENT_BACKEND_URL=http://localhost:3000
CODING_AGENT_USE_SYSTEMD=1
CODING_AGENT_MODEL_ID=$MODEL_ID
VITE_API_SERVER=https://$HOST
VITE_CODING_AGENT_WS_URL=wss://$HOST/ws
VITE_HMR_WSS=1
PROD_API_SERVER=$PROD_API_SERVER
EOF
chown ec2-user:ec2-user /home/ec2-user/.config/dev-box.env
chmod 600 /home/ec2-user/.config/dev-box.env

# ---- Caddy: new Caddyfile, drop the old certificate state, (re)start ----
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
    handle /ws* {
        reverse_proxy 127.0.0.1:8765
    }
    handle /dev-box/* {
        reverse_proxy 127.0.0.1:8000
    }
    handle /api/* {
        reverse_proxy 127.0.0.1:3000
    }
    handle {
        reverse_proxy 127.0.0.1:5173
    }
}
EOF
chown root:caddy /etc/caddy/Caddyfile
chmod 640 /etc/caddy/Caddyfile
systemctl stop caddy || true
rm -rf /var/lib/caddy/.local/share/caddy
systemctl enable --now caddy
