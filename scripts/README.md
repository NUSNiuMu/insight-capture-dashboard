# Scripts Layout

This directory is organized by responsibility:

- `dashboard/`: user-facing dashboard launchers
- `docker/`: Docker build, run, shell, and container entrypoint helpers
- `dev/`: environment checks and local developer utilities
- `*.py`: core Python application code shared by the launchers

Typical entrypoints:

- `scripts/dashboard/open_monitor_dashboard.sh`
- `scripts/dashboard/open_web_dashboard.sh`
- `scripts/dashboard/open_web_3d_right.sh`
- `scripts/docker/build.sh`
- `scripts/docker/run.sh`
- `scripts/docker/enter.sh`
- `scripts/dev/check_env.sh`
