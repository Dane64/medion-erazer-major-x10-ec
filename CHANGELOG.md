# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). The release workflow publishes the
section whose heading matches the tag, so keep entries user-facing.

## [Unreleased]

### Added

- Secure Boot support through the signed `x10_ec` kernel module (DKMS + MOK).
- `x10ctld` root daemon; the GUI now runs as a normal user in the `x10ctl` group.
- CPU, GPU, display, audio and lighting pages next to the fan dashboard.
- Application icon, sidebar logo, navigation icons and the Outfit brand typeface.
- `arc-dgpu-ctl` is bundled as a submodule and installed by `scripts/install.sh`.
- Release notes are taken from this changelog.

### Changed

- Uniform page headers and Erazer black-and-blue styling across all pages.
- The installer builds from a temporary copy, so it no longer leaves root-owned
  files in the source checkout.
- Release source archives now include everything `scripts/install.sh` needs.

### Fixed

- Telemetry graph no longer fills the area under the temperature lines.

[Unreleased]: https://github.com/Dane64/medion-erazer-major-x10-ec/commits/main
