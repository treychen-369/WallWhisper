# TOOLS.md — Emily's Environment Notes

## Smart Home Setup

### Camera & Speaker
- **Device:** EZVIZ C7 (or compatible EZVIZ camera with speaker)
- **Location:** Living room (or wherever you want Emily to interact)
- **Camera IP:** YOUR_CAMERA_IP (LAN IP, e.g. 192.168.1.100)
- **Channels:** Depends on camera model (C7 has 2: wide angle + PTZ)
- **Audio Output:** Speaker via RTSP Backchannel (AAC, 16kHz)
- **Audio Input:** Built-in microphone

### Home Gateway (Smart Home Hub)
- **Device:** Router or mini PC running Emily in Docker
- **Router IP:** YOUR_ROUTER_IP (e.g. 192.168.1.1)
- **Emily Container:** Monitors EZVIZ alerts → calls OpenClaw API → TTS → Camera speaker

### Network
- **Wi-Fi SSID:** YOUR_WIFI_SSID
- **Security:** WPA2 + AES (recommended)

## Interaction Flow

```
EZVIZ Camera detects person
  → Router (Emily container) receives alert
  → Sends context to OpenClaw Emily Agent
  → Emily generates English content
  → Router does TTS + pushes audio to camera speaker
  → Family hears Emily speaking!
```

## Weather
- **Location:** Your city/region
- Use weather skill to get current conditions for contextual greetings

## Time Zones
- **Family timezone:** Asia/Shanghai (or your timezone, e.g. America/New_York)
- All times in messages are local time
