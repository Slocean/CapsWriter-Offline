# CapsWriter Desktop (Windows)

Run `desktop/build.ps1` in Windows PowerShell, then place `CapsWriterDesktop.exe` beside a CapsWriter v2.6 `start_client.exe` and the modified `core/` source tree. Double click the desktop EXE. The GUI owns the tray icon and optional floating recorder; the original Python client runs hidden in the background.

The GUI saves the server address, port, CapsLock switch and segment duration in `config_client.py` (with a `.bak` backup), enables loopback UDP control and progressive typing, and restarts its owned backend on Save. Closing the main window hides it to the tray. Exit in the tray stops the backend.

Progressive typing sends a partial result after roughly the configured segment duration plus one second of overlap, then updates the same text as recognition revises it. It uses backspace and typing in the current foreground window. Keep the caret at the end of the inserted text. If focus changes, later revisions are stopped to avoid editing another app; the final result remains in the GUI log. Applications that intercept keystrokes, secure input fields, or remote desktops may not support this behavior reliably. Shorter segments can reduce recognition accuracy and increase server load. This is segmented recognition, not character-by-character streaming.

The server remains the upstream CapsWriter server; no server code change is required for partial results. Each client configures its own server address. Use this only on a trusted LAN unless you add transport authentication and encryption.
