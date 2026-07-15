#!/usr/bin/env bash
# Install daily memory soak + weekly full auto prove as user systemd timers on Banana.
set -euo pipefail
ROOT="${WW_HOME:-$HOME/worldwave}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

cat >"$UNIT_DIR/ww-memory-soak.service" <<EOF
[Unit]
Description=WW memory multi-day soak tick
After=network-online.target ww.service

[Service]
Type=oneshot
WorkingDirectory=$ROOT
EnvironmentFile=-$ROOT/.env
Environment=WW_PROVE_URL=http://127.0.0.1:%WW_PORT%
# WW_PORT expanded poorly; set explicitly in ExecStart env
ExecStart=/bin/bash -lc 'set -a; source $ROOT/.env; set +a; export WW_PROVE_URL=http://127.0.0.1:\${WW_PORT:-9302}; $ROOT/.venv/bin/python $ROOT/scripts/memory_soak.py'
Nice=10
EOF

# fix service file - don't use %WW_PORT%
cat >"$UNIT_DIR/ww-memory-soak.service" <<'EOF'
[Unit]
Description=WW memory multi-day soak tick
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/worldwave
ExecStart=/bin/bash -lc 'set -a; source %h/worldwave/.env; set +a; export WW_PROVE_URL=http://127.0.0.1:${WW_PORT:-9302}; %h/worldwave/.venv/bin/python %h/worldwave/scripts/memory_soak.py'
Nice=10
EOF

cat >"$UNIT_DIR/ww-memory-soak.timer" <<'EOF'
[Unit]
Description=Daily WW memory soak

[Timer]
OnCalendar=*-*-* 04:15:00
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
EOF

cat >"$UNIT_DIR/ww-memory-auto.service" <<'EOF'
[Unit]
Description=WW memory full automated prove suite
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/worldwave
Environment=WW_PROVE_SKIP_L0=1
Environment=WW_PROVE_ALLOW_RESTART=1
ExecStart=/bin/bash -lc 'set -a; source %h/worldwave/.env; set +a; export WW_PROVE_URL=http://127.0.0.1:${WW_PORT:-9302}; export WW_PROVE_SKIP_L0=1; export WW_PROVE_ALLOW_RESTART=1; bash %h/worldwave/scripts/memory_auto_all.sh'
Nice=10
EOF

cat >"$UNIT_DIR/ww-memory-auto.timer" <<'EOF'
[Unit]
Description=Weekly WW memory auto prove

[Timer]
OnCalendar=Sun *-*-* 05:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now ww-memory-soak.timer
systemctl --user enable --now ww-memory-auto.timer
systemctl --user list-timers --all | grep ww-memory || true
echo "Installed ww-memory-soak.timer (daily) and ww-memory-auto.timer (weekly)"
