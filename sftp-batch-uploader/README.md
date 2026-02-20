# SFTP Batch Uploader

## Overview

**SFTP Batch Uploader** is a lightweight Windows desktop application (built with Python + Tkinter) that lets you upload batches of files to any SFTP server through a simple GUI. It is distributed as a single standalone `.exe` — no Python installation required.

---

## Features

- 🔑 **Password or SSH key authentication** — supports any key type (RSA, Ed25519, ECDSA, etc.)
- 💾 **Connection presets** — save, load, delete and set a default connection profile (stored in `sftp_presets.json` next to the exe)
- 📂 **Flexible file selection** — add individual files or an entire folder (CSV files auto-detected)
- ⏱ **Configurable delay** between uploads (seconds) — avoids overwhelming the server
- 🧪 **Test batch mode** — pause after the first N files and confirm before continuing
- ⏳ **Scheduled start delay** — set a countdown (in minutes) before the upload begins, with a warning if the delay exceeds 30 minutes
- 🔄 **Auto-reconnect** — detects dropped connections and reconnects automatically during long delays
- 📋 **Live log** — real-time countdown timer bar and scrollable log output
- ⏹ **Cancel at any time** — graceful stop mid-upload

---

## How to Use

### Option A — Run the executable (no Python needed)

1. Download `sftp_gui.exe` from this folder
2. Double-click to run — no installation required

### Option B — Run from source

```bash
pip install paramiko
python sftp_gui.py
```

---

## Connection Tab

| Field      | Description                           |
| ---------- | ------------------------------------- |
| Host       | SFTP server hostname or IP            |
| Port       | Default: `22`                         |
| Username   | SFTP login username                   |
| Auth type  | `Password` or `SSH Key`               |
| Password   | Plaintext password (hidden input)     |
| Key file   | Path to your private key file         |
| Remote dir | Remote directory to upload files into |

Use **Test Connection** to verify credentials before uploading.

---

## Options Tab

| Option                | Description                                                         |
| --------------------- | ------------------------------------------------------------------- |
| Delay between uploads | Wait N seconds between each file upload                             |
| Test batch            | Pause after uploading the first N files and ask whether to continue |
| Start delay           | Wait N minutes before the upload process begins                     |

---

## Building the Executable Yourself

Requires Python 3.10+ and pyinstaller:

```bash
pip install paramiko pyinstaller
pyinstaller --noconsole --onefile sftp_gui.py
# Output: dist/sftp_gui.exe
```

---

## Requirements (source only)

- Python 3.10+
- `paramiko` >= 4.0

Built-in modules used: `tkinter`, `threading`, `queue`, `json`, `os`, `datetime`
