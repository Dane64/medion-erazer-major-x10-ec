# Contributing

Changes should improve the supported application without weakening its hardware
guards or introducing unverified commands. Start with the [user guide](docs/user-guide.md)
and [protocol reference](docs/protocol.md).

## Development

Use Python 3.11 or newer and `uv`. Install dependencies without administrator
privileges:

```bash
uv sync --locked --group build
uv run --no-sync medion-fan-control --demo
uv run --no-sync python -m unittest discover -s tests -v
```

The test suite uses Python's standard-library `unittest`. GUI tests use Qt's
offscreen platform, a simulated controller, and mocked hardware boundaries.
Transport tests use temporary files and fake byte I/O. No test requires root
or opens real EC/HID devices.

For a focused change, select the relevant test module:

```bash
uv run --no-sync python -m unittest discover -s tests -p test_protocol.py -v
```

Do not use live hardware as an automated test fixture. Demo mode must not read
or write hardware or persisted user settings.

## Source organization

| Module | Responsibility |
| --- | --- |
| `cli.py`, `__main__.py` | Launch arguments, version output, and application startup |
| `gui.py` | Window composition, confirmations, asynchronous work, and visible state |
| `widgets.py`, `theme.py` | Reusable Qt widgets, graph rendering, and presentation |
| `telemetry.py` | Bounded, monotonic in-memory history |
| `controller.py` | Session lifecycle, simulated controller, and apply-then-save orchestration |
| `protocol.py` | Vendor commands, timing, retries, and verified readback |
| `hardware.py` | DMI/security/power guards, locking, and byte-I/O transport |
| `lighting.py` | RGB values, verified HID discovery, and report transport |
| `settings.py` | Validated loading and atomic, owner-only settings persistence |
| `tools/release.py` | Release tag, distribution-content, and checksum validation |

Keep Qt out of the protocol, transport, session, and settings layers. All EC
session operations, including opening and closing, run on one serialized
worker. HID writes and settings persistence use a separate serialized worker.
Worker completion must not overwrite a newer pending operation's UI state.

Treat a failed firmware readback as unknown state, not a successful change or
proof that the old state is still current. Surface expected failures with
actionable messages. Release open resources on failure, and never silently
default to a permissive hardware or power state.

Keep the supported operations narrow. Add regression tests for changed
behavior, including cancellation, failure, and cleanup paths. Document
user-visible changes and protocol evidence alongside the implementation.

## Dependencies and packaging

Commit `pyproject.toml` and `uv.lock` together after intentional dependency
changes. `uv lock` refreshes the lock file; ordinary development and CI use
`--locked` to reject drift. The `build` group locks the backend and its
transitive dependencies for release builds.
The local build cache also tracks Git commits and tags so switching revisions
or adding a version tag refreshes installed version metadata.

```bash
uv sync --locked --group build
uv build --no-build-isolation
uv run --no-sync python tools/release.py check dist
```

Start with an empty output directory, or use `uv build --out-dir` and pass that
same directory to the package checker. Versioning comes from Git tags. A source
checkout without Git metadata is not a substitute for a published sdist.

The sdist includes source, tests, documentation, the lock file, and the release
checker. The wheel includes only the application and package metadata.
Generated files, local captures, obsolete research utilities, and logs do not
belong in either distribution.

## Pull requests

Explain the user-visible outcome and why the change is needed. Include the
relevant regression coverage and update documentation when behavior changes.
Do not include vendor binaries, firmware dumps, serial numbers, local build
artifacts, or unrelated formatting churn.

CI runs the application and release-checker tests on Python 3.11 through 3.14,
builds a wheel from the sdist, checks package contents, and exercises the
installed wheel outside the checkout. The **Quality gate** job is the required
aggregate result. Maintainer publishing instructions are in
[Releases](docs/releases.md).
