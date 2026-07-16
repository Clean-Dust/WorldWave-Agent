# Install `ww.service` (user systemd unit)

Repo template: [`deploy/ww.user.service`](../deploy/ww.user.service).

## One-liner (from install root)

```bash
WW_HOME="${WW_HOME:-$HOME/worldwave}"
mkdir -p ~/.config/systemd/user
sed "s|@WW_HOME@|$WW_HOME|g" "$WW_HOME/deploy/ww.user.service" \
  > ~/.config/systemd/user/ww.service
systemctl --user daemon-reload
systemctl --user enable --now ww.service
# optional: keep running after logout
loginctl enable-linger "$USER"
```

## Via deploy.sh

- On install/update, `deploy.sh` refreshes the unit file if systemd user bus is available.
- To also enable/start: `WW_SYSTEMD_ENABLE=1 bash deploy.sh` (or update).

Requirements: `WorkingDirectory` = worldwave root, `ExecStart` = `.venv/bin/python server.py`,
`EnvironmentFile` = `.env`, `Restart=always`.

Do **not** print secrets from `.env` when debugging the unit.
