# Changelog

All notable changes to `mushy` are documented in this file.

## [0.3.0] - 2026-11-05

### Renamed
- The project is now **Mushy** (`pip install mushy`, command `mushy`, import `mushy`). It was previously published as `plexo-cli`. It is an independent project and is not affiliated with the Plexo desktop app.
- The user data folder is now `~/.mushy` instead of `~/.plexo`.
- Everything from 0.2.2 below is included (SSRF guard, interface pinning, proxy round-robin, Python CI, trusted publishing).

## [0.2.2] - 2026-11-02

### Security
- **SSRF guard:** The proxy now resolves every destination and rejects non-public addresses (loopback, private, link-local incl. cloud metadata `169.254.169.254`, CGNAT, multicast, reserved, IPv4-mapped IPv6) with HTTP 403. The validated IP literal is used for the connection, which prevents DNS rebinding. Opt out with `mushy proxy --allow-private`.
- **Port filter on plain HTTP:** The 80/443 allow-list previously applied only to `CONNECT`; it now also covers plain HTTP forwarding.
- **Credential leak fixed:** `Proxy-Authorization` and `Proxy-Connection` headers are no longer forwarded to origin servers.
- **IPv6 CONNECT targets** (`[::1]:443`) are now parsed correctly.

### Fixed
- **Interface pinning was not applied to real traffic.** `SO_BINDTODEVICE` (Linux) / `IP_BOUND_IF` (macOS) was used only by the connectivity probe; downloads and the proxy bound by source IP alone. Both now pin sockets to the adapter (`BoundRangeClient(iface_name=...)` and the proxy's bound connections, including plain-HTTP forwarding).
- If device pinning is unavailable (e.g. no `CAP_NET_RAW` on Linux < 5.7) Mushy degrades to source-IP binding and logs a one-time warning instead of failing or silently using the default route. `bind_socket_to_interface` now returns how the socket was bound (`device` / `source-ip` / `none`).

### Packaging & CI
- Added Python CI (3.9-3.14: ruff, pytest, pip-audit, build + `twine check`); publishing now requires it to pass and verifies the tag matches the package version.
- PyPI publishing uses OIDC trusted publishing only; the long-lived `PYPI_API_TOKEN` is no longer used.
- Fixed project URLs (changelog path) and author email metadata.
- Added a `[tool.ruff]` correctness gate and removed dead code/unused imports.

### Proxy
- Connections are now distributed round-robin across all detected adapters (previously always the first adapter).
- **Breaking:** `--allow-remote` is deprecated and ignored. Binding to a non-loopback host always requires `--auth user:pass`; an unauthenticated remote proxy is no longer possible.

### Project hygiene
- `Homepage` and `Issues` now point to this repository (they previously referenced another project's site and tracker).
- The adapter-quota test is skipped where `127.0.0.2` is not bindable (macOS) instead of failing.

### Behavior change
- Plain-HTTP proxying to ports other than 80/443 is blocked. Use `--allow-private` only for private *addresses*; there is no port override yet.

## [0.2.0] - 2026-10-31

### Security
- **Proxy Authentication:** Refuse non-loopback `--host` bindings unless `--auth user:pass` or `--allow-remote` is specified.
- **Timing-Safe Auth:** Validate `Proxy-Authorization` Basic header via `hmac.compare_digest` to prevent side-channel timing attacks. Returns HTTP 407 on failure.
- **SSRF Prevention:** Restrict HTTP `CONNECT` tunneling strictly to ports 80 and 443 by default.

### Correctness & Networking
- **Multi-OS Socket Binding:** Added `bind_socket_to_interface` using `SO_BINDTODEVICE` on Linux and `IP_BOUND_IF` on macOS to eliminate default-gateway routing bottlenecks.
- **Diagnostic Engine:** Added `mushy doctor` command to verify real-time per-NIC throughput deltas.
- **BitTorrent Engine:** Enforced SHA-1 piece verification and marked CLI feature as experimental (`--experimental`).

### Packaging & Performance
- **Modular Extras:** Core install is featherlight. Video engine dependencies (`yt-dlp`, `curl_cffi`, `imageio-ffmpeg`) moved to optional extra `mushy[video]`.
- **System FFmpeg:** Automatically detect and utilize system `ffmpeg` on PATH before falling back to bundled binary.
- **PyPI Trusted Publishing:** Added GitHub Actions OIDC workflow for automated, tokenless releases.

### Compliance
- **Fair Use Notice:** Added one-time educational & compliance disclaimer on first run of `mushy video`.
- **DRM Policy:** Explicitly disclaimed DRM/paywall circumvention in documentation and CLI.
