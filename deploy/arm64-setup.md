# ARM64 Deployment Guide

**For:** Raspberry Pi 4/5, NVIDIA Jetson Orin Nano Super

---

## Docker on Raspberry Pi

### 1. Install Docker (if not present)
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### 2. Install Docker Compose
```bash
sudo apt-get update
sudo apt-get install -y docker-compose-plugin
```

### 3. Enable Docker on Boot
```bash
sudo systemctl enable docker
```

---

## Jetson Orin Nano Super Notes

The Jetson runs Ubuntu 22.04 with L4T (Linux for Tegra).

### Docker Differences
- Use `nvidia-docker2` for GPU containers (not needed for this project)
- Standard Docker works fine for CPU-only services
- NVMe SSD recommended over microSD for database I/O

### Performance Tuning
```bash
# Disable swap (prevents SD card wear)
sudo dphys-swapfile swapoff
sudo systemctl disable dphys-swapfile

# Mount /tmp as tmpfs (RAM disk)
echo "tmpfs /tmp tmpfs defaults,nosuid,size=512M 0 0" | sudo tee -a /etc/fstab
```

---

## Building ARM64 Images

Our Dockerfile uses `python:3.11-slim-bookworm` which has ARM64 wheels.
No special build flags needed.

```bash
docker compose build --no-cache
```

Expected build time on Raspberry Pi 4:
- First build: ~8 minutes
- Subsequent builds (cached): ~30 seconds

---

## Startup Script

Create `/etc/systemd/system/sms-agent.service`:

```ini
[Unit]
Description=SMS Tuition Agent
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/sms-tuition-agent
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
User=pi

[Install]
WantedBy=multi-user.target
```

Enable:
```bash
sudo systemctl enable sms-agent
sudo systemctl start sms-agent
```

---

## USB Backup (Hermes Integration)

If using Hermes Agent for backup:
```bash
# Hermes will detect the USB drive labeled 'BACKUP USB'
# and run the backup script automatically at 02:15 PT
# Ensure HERMES_BACKUP_USB_LABEL is set in environment
```
