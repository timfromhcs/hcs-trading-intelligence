# Desktop development builds

Build on the same operating system and architecture as the target. The project uses PyInstaller's one-folder format at this stage. [PyInstaller documentation](https://pyinstaller.org/en/stable/usage.html)

```sh
npm ci --prefix frontend
npm run build --prefix frontend
uv run --locked --group packaging python scripts/build_desktop.py
uv run --locked python scripts/smoke_desktop.py
uv run --locked python scripts/archive_desktop.py
```

The executable and its bundled libraries live together under `dist/desktop/HCS-Trading-Intelligence`. Keep the entire folder when moving it. Launching the executable without arguments starts the loopback server and opens its dashboard in the default browser. Use `serve` to start without opening a browser. Local application data stays in the operating system's data directory by default. `serve --data-dir PATH` selects a separate directory.

CI builds and smoke-tests the native folder on Windows, macOS and Linux and uploads a compressed archive with a SHA-256 checksum.

Windows also supports a single EXE. After verifying the one-folder build, run the same three Python build, smoke-test and checksum commands with `--onefile`. The executable and checksum are under `dist/desktop-single`. Windows CI uploads these files separately.

The artifacts are development builds. Signing, notarization and production release verification remain separate milestones.
