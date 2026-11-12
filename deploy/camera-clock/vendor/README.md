# Offline camera clock runtime

The ARM64 Ubuntu 22.04 package `chrony_4.2-2ubuntu2_arm64.deb` is copied from the
existing verified camera runtime. SHA256:
`557e4ec0d51f8363945dea712e172a9b9e0a3648e685b72ad85de2629c3182d8`.

Corresponding upstream and Debian packaging sources are included alongside it,
retrieved from `https://ports.ubuntu.com/pool/main/c/chrony/` on 2026-11-12.
`COPYRIGHT` and `LICENSE` preserve licensing. Install extracts this owned package
without apt, network access, or another project. System libraries are supplied by
the camera's Ubuntu image; installation rejects missing dynamic dependencies.

This is the Ubuntu camera adapter, not the Buildroot/X5 clock implementation.
