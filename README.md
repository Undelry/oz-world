# OZ - Virtual World Dashboard

Summer Wars-inspired 3D virtual world that visualizes AI agent activity in real-time.

## Overview

OZ is a Three.js-based 3D space where AI agents (Claude Code CLI workers) are represented as floating entities. It combines a management dashboard with an interactive virtual world, displaying real-time worker states, task progress, and system health.

## Architecture

```
Browser (Three.js)
  |
  v
OZ Server (Python HTTP, port 8766)
  |
  v
oz_webserver.py (WebSocket, port 8767)
  |
  v
OpenClaw workspace data (worker states, task queue, screenshots)
```

## Files

| File | Description |
|------|-------------|
| `index.html` | Main frontend - Three.js 3D world with dashboard overlays |
| `server.py` | HTTP server serving static files + live hitomi screenshots |
| `three.min.js` | Three.js library (r152) |

## Features

- 3D virtual space with floating agent entities
- Real-time worker state visualization (idle/working/error)
- Management dashboard overlay
- Live hitomi screenshot feed
- Upwork job cache display
- Mobile-responsive with touch controls

## Running

```bash
cd ~/Desktop/OZ
python3 server.py
# Open http://localhost:8766
```

The full OZ system also requires `oz_webserver.py` from the [hitomi-workspace](https://github.com/Undelry/hitomi-workspace) for WebSocket real-time updates.

## Related

- [hitomi-workspace](https://github.com/Undelry/hitomi-workspace) - Agent orchestration system
- `oz_webserver.py` - WebSocket server for real-time agent data
- `oz_agent_monitor.py` - Worker state monitoring
- `oz_launcher.py` - Auto-launch OZ in Arc browser

## License

MIT License - Undelry Inc.
