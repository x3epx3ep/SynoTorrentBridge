# SynoTorrentBridge

A tiny Windows utility that intercepts **magnet links** and **.torrent files** and sends them straight to **Synology Download Station** — with folder and file selection *before* the download starts.

[Русская версия](README.ru.md) · [Changelog](CHANGELOG.md)

## Why

Synology's own tools don't cover this well on desktop:

- **DS get** is Android/iOS only.
- Browser extensions can't pick individual files before the download starts.
- Most third-party clients (e.g. Electorrent) use the legacy DSM 6 API, don't support **2FA / OTP**, and add the whole torrent without asking.

SynoTorrentBridge was built to fix exactly that.

## Features

- **Intercepts magnet:// links and .torrent files** (registered per-user — no admin rights needed). Turning it off **restores the previous handler** (e.g. qBittorrent) if there was one.
- **Choose the destination folder** on the NAS for every download (folder tree is read via FileStation). A folder that doesn't exist yet can be created on the fly.
- **Choose which files to download before anything starts**:
  - `.torrent` — uses Download Station's *pending list* API (`create_list` + `Task.List.Polling download selected=[…]`), so the task is created with only your files wanted.
  - magnet — the task is created; the moment metadata arrives all files except the smallest one are unwanted (unwanting *all* of them makes Download Station hang in "Checking"), then only the ones you tick stay wanted.
- **Search & filters** in the file picker: text search, "Only video", "Without subtitles", select all/none, live size counter. Downloading nothing is not allowed — use Cancel.
- **Full 2FA support** (TOTP) with a *trusted device* — the code is asked once, not on every run.
- **Duplicate check** — warns if the same torrent is already in Download Station (by info-hash for magnet tasks, by exact name otherwise). The app never touches an existing task it merely *thinks* is the same.
- **Completion notifications** — an optional background monitor shows a toast when a download finishes, including tasks that go straight to seeding.
- **Auto-update** — checks GitHub Releases from the settings dialog, verifies the download against the **SHA-256 published by GitHub**, and replaces itself even while the background monitor is running (the monitor is restarted afterwards).
- **Self-healing paths** — if you move the exe or download it again as `SynoTorrentBridge (2).exe`, the handlers, autostart and "Apps" entry are re-pointed to the copy you started.
- **Sharp on 4K / high-DPI screens** — the window follows Windows display scaling.
- **Single .exe, no dependencies** — no Python, no installation required.
- **Clean uninstall** — appears in Windows "Apps & Features"; uninstall stops the monitor, removes handlers (restoring previous ones), autostart, optionally settings, and the exe itself.

## Installation

1. Download `SynoTorrentBridge.exe` from [Releases](../../releases) and put it in a permanent folder (e.g. `C:\Tools\SynoTorrentBridge\`).
2. Double-click it — the settings dialog opens. Enter the NAS address, login and password. HTTPS on port 5001 is the default; uncheck "Verify SSL certificate" only if your DSM uses a self-signed certificate. If 2FA is enabled, you'll be asked for the OTP code once — the device is remembered afterwards.
3. Click **Save**, then **Enable** in "Windows integration".
4. Optionally enable the **completion monitor** (runs hidden at logon, shows notifications).

> Windows may show a SmartScreen warning on first start: the exe is not code-signed. Check the SHA-256 in the release notes against the file you downloaded (`Get-FileHash SynoTorrentBridge.exe`).

## Usage

- Click any magnet link or open a .torrent file → the dialog pops up → pick folder and files → done. The download starts on your NAS.

Command line:

```
SynoTorrentBridge.exe "magnet:?xt=..."     add a magnet link
SynoTorrentBridge.exe "file.torrent"       add a .torrent file
SynoTorrentBridge.exe --settings           settings
SynoTorrentBridge.exe --register           enable interception
SynoTorrentBridge.exe --unregister         disable interception (restores the previous handler)
SynoTorrentBridge.exe --uninstall          remove everything
```

## Security notes

- Config lives in `%APPDATA%\SynoTorrentBridge\config.json`; the password is stored encrypted with **Windows DPAPI** (tied to your user account).
- Login is sent as a **POST** request, so the password never appears in URLs or web-server logs. Use HTTPS (default) so it is not sent in clear text over the network.
- Updates are accepted only if their SHA-256 matches the digest GitHub publishes for the release asset.
- Registration is per-user (`HKCU`) — no admin rights, no system-wide changes.
- The log `%APPDATA%\SynoTorrentBridge\bridge.log` is rotated at 1 MB (3 old copies kept) and contains no passwords.

## Building from source

Requires Python 3.10+. Two optional runtime dependencies for certificate checks: [truststore](https://pypi.org/project/truststore/) (verification by Windows itself) and [certifi](https://pypi.org/project/certifi/) (Mozilla's CA list as a fallback); packaging also needs PyInstaller and Pillow:

```
python -m pip install truststore certifi pyinstaller pillow
build_exe.bat
```

The result is `dist\SynoTorrentBridge.exe` (version info embedded; the script prints its SHA-256).

## Running tests

```
python tests\test_all.py          parser, API client vs mock NAS, monitor, updater, registry, uninstall, log
python tests\gui_smoke.py         dialog layout
python tests\gui_flow_test.py     add-torrent dialog flow (empty selection, missing folder, task creation)
python tests\tls_fallback_test.py   certificate fallback (local HTTPS with a throwaway CA)
python tests\magnet_select_test.py  magnet file selection never leaves zero wanted files
python tests\otp_flow_test.py     2FA prompt
python tests\e2e_live.py          read-only check against YOUR configured NAS (nothing is downloaded)
python tests\e2e_magnet_live.py   magnet flow on YOUR NAS with the open Sintel torrent (task and folder removed afterwards)
```

Tests never touch the real handlers, settings or a running monitor: they use their own registry branch and Windows object names.

## Tech notes

- Standard library plus `truststore` and `certifi`: `tkinter` GUI, `urllib` HTTP, `winreg`/`ctypes` for Windows integration.
- HTTPS certificates are verified by Windows itself (via `truststore`), so corporate and manually installed roots work and missing roots are downloaded on demand. If Windows still reports an untrusted root (e.g. root auto-update disabled by group policy — Chrome and Edge don't notice, they ship their own CA list), the check is repeated against the Windows store plus Mozilla's CA list (`certifi`). A publicly trusted certificate such as a Let's Encrypt wildcard on `dsm.example.com` is therefore accepted; self-signed certificates and wrong host names are still rejected.
- DPI-aware: the UI scales with Windows display scaling (125–200 %, 4K).
- Talks to Download Station via the DSM 7 Web API (`SYNO.DownloadStation2.*`): `Task create` with `create_list`, `Task.List`, `Task.List.Polling download` with `selected=[...]`, `Task.BT.File list/set` for magnets. Task status `5` = finished, `8` = finished and seeding.
- Tested on Windows 10/11 with DSM 7.x / Download Station 4.x.

## License

MIT — see [LICENSE](LICENSE).
