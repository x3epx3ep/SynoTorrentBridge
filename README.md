# SynoTorrentBridge

A tiny Windows utility that intercepts **magnet links** and **.torrent files** and sends them straight to **Synology Download Station** — with folder and file selection *before* the download starts.

[Русская версия](README.ru.md)

## Why

Synology's own tools don't cover this well on desktop:

- **DS get** is Android/iOS only.
- Browser extensions can't pick individual files before the download starts.
- Most third-party clients (e.g. Electorrent) use the legacy DSM 6 API, don't support **2FA / OTP**, and add the whole torrent without asking.

SynoTorrentBridge was built to fix exactly that.

## Features

- **Intercepts magnet:// links and .torrent files** system-wide (registered as the default handler, per-user — no admin rights needed).
- **Choose the destination folder** on the NAS for every download (folder list is read from Download Station).
- **Choose which files to download before anything starts**:
  - `.torrent` — uses Download Station's *pending list* API (`create_list`), so the task is created only after you confirm the selection.
  - magnet — the task is created paused-for-selection: all files are unwanted first, then only the ones you tick are re-enabled.
- **Search & filters** in the file picker: text search, "Only video", "Without subtitles", select all/none, live size counter.
- **Full 2FA support** (TOTP) with a *trusted device* — the code is asked once, not on every run.
- **Duplicate check** — warns if the same torrent (by info-hash or title) is already in Download Station.
- **Completion notifications** — an optional background monitor shows a toast when a download finishes.
- **Single .exe, no dependencies** — no Python, no installation required. Just download and run.
- **Clean uninstall** — appears in Windows "Apps & Features"; uninstall removes protocol handlers, autostart, config and the exe itself.

## Installation

1. Download `SynoTorrentBridge.exe` from [Releases](../../releases) and put it anywhere (e.g. `C:\Tools\`).
2. Run it once with settings:

   ```
   SynoTorrentBridge.exe --settings
   ```

   Enter your NAS address, login and password. If 2FA is enabled, you'll be asked for the OTP code once — the device is remembered afterwards.
3. Enable interception of magnet links and .torrent files in the settings dialog.
4. Optionally enable the **completion monitor** (runs hidden at logon, shows notifications).

## Usage

- Click any magnet link or open a .torrent file → the dialog pops up → pick folder and files → done. The download starts on your NAS.
- `--settings` — open settings.
- `--uninstall` — remove everything (handlers, autostart, config, the exe itself).

## Security notes

- Config lives in `%APPDATA%\SynoTorrentBridge\config.json`; the password is stored encrypted with **Windows DPAPI** (tied to your user account).
- Registration is per-user (`HKCU`) — no admin rights, no system-wide changes.

## Building from source

Requires Python 3.10+ (standard library only, plus PyInstaller for packaging):

```
pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name SynoTorrentBridge --icon icon.ico main.py
```

The result is `dist\SynoTorrentBridge.exe`.

## Running tests

```
python tests\test_all.py
```

Unit tests mock the network; `tests/gui_smoke.py` opens the real dialogs for a screenshot check.

## Tech notes

- Pure standard library: `tkinter` GUI, `urllib` HTTP, `winreg`/`ctypes` for Windows integration.
- Talks to Download Station via the DSM 7 Web API (`SYNO.DownloadStation2.*`): `Task create` with `create_list`, `Task.List`, `Task.List.Polling download` with `selected=[...]`, `Task.BT.File list/set` for magnets.
- Tested on Windows 10/11 with DSM 7.x.

## License

MIT — see [LICENSE](LICENSE).
