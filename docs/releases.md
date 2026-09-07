# Releases

[Back to the project overview](../README.md)

Packages are published to GitHub Releases, not PyPI. Every release contains
exactly a wheel, a source distribution, and `SHA256SUMS`.

## One-time repository setup

Workflow files alone cannot enforce repository protection. Before publishing,
maintainers should configure these GitHub settings:

| Setting | Required policy |
| --- | --- |
| Default branch ruleset | Require pull requests and the **Quality gate** status check before merging |
| Tag ruleset for `v*` | Restrict tag creation to maintainers; prevent updating or deleting published version tags |
| Release settings | Enable release immutability to protect published tags and assets from manual replacement as well |
| `release` environment | Configure required reviewers and allow deployment only from protected version tags |
| Actions permissions | Enable the pinned Actions used by the workflows and permit the release job's `contents: write` token |

The release job names the `release` environment but cannot add its approval
rules. Without repository-side configuration, environment approval is not
enforced. Dependabot opens weekly updates for Actions and locked Python
dependencies; review those through the same CI pipeline. Review the pinned
`uv` version in both CI setup steps when updating build tooling.

## Version policy

Hatch VCS derives the package version from Git. Accepted release tags are:

```text
v0.1.0
v0.2.0a1
v0.2.0b1
v0.2.0rc1
```

Use `vMAJOR.MINOR.PATCH`, optionally followed by `aN`, `bN`, or `rcN`, without
leading zeroes in numeric components. Prerelease tags produce GitHub
prereleases. Development or dirty-checkout versions are valid for local
builds but cannot pass the version check for a release tag.

Do not move or reuse a published tag. Fix a broken release with a new version.

## Prepare and publish

Start from a clean, up-to-date default branch containing the reviewed changes.
The release workflow rejects tags whose commits are not on that branch.
Use an empty `dist/` directory or choose another output directory.

```bash
uv sync --locked --group build
uv run --no-sync python -m unittest discover -s tests -v
uv build --no-build-isolation
uv run --no-sync python tools/release.py check dist
```

Create and push the intended annotated version tag. For example:

```bash
git tag -a v0.1.0 -m "Release 0.1.0"
git push origin v0.1.0
```

Use a signed tag instead where your maintainer signing setup supports it.
Do not publish a local development build manually: the workflow rebuilds the
tagged source and publishes only its validated artifacts.

## Automated pipeline

| Stage | Safeguards |
| --- | --- |
| Tag validation | Canonical version format and default-branch ancestry |
| Shared CI | Same reusable workflow as branch pushes and pull requests; locked runtime dependencies and Python 3.11-3.14 tests |
| Build | Locked build group, no build isolation that could resolve newer backend dependencies, complete Git history, and `SOURCE_DATE_EPOCH` from the source commit |
| Package validation | Matching tag/wheel/sdist versions, application modules, launcher, license, maintained source files, and no generated or unrelated archive content |
| Installed-package exercise | Fresh environment with hash-locked runtime dependencies; launcher and GUI tests run outside the source checkout |
| Publication | Approved environment when configured, artifact checksum verification, unchanged remote tag, complete draft assets, then publication |

The default `uv build` operation builds the sdist first and the wheel from that
sdist. This exercises the source package without depending on files available
only in the checkout. The release workflow downloads those same checked
artifacts; it does not build a second, different package during publication.

Jobs have timeouts. Ordinary CI cancels superseded branch runs, while release
runs are serialized per tag and are not cancelled mid-publication. Actions are
pinned to immutable commits. Only the publication job has write access, and it
does not check out or execute project source.

## Failed runs and retries

A failure in the shared CI pipeline prevents publication. A failed upload
leaves a draft rather than exposing a partial public release. Re-run failed
jobs after resolving transient infrastructure problems; a retry can replace
assets in the existing draft and publishes only when its asset list matches
the expected three files.

If a draft has extra manually uploaded assets, remove those extras from the
draft before retrying. The workflow will not publish an unexpected asset set.
Already-published releases are never overwritten by a re-run.

Artifacts are retained by Actions for seven days. If they have expired, rerun
the complete release workflow so validation and packaging execute again. If
source or dependency changes are required, merge the fix and create a new tag
instead of retargeting the failed one.

## Download verification and installation

Download both the desired package and `SHA256SUMS` from the same release.
Downloading both distributions allows the complete checksum check:

```bash
sha256sum --check SHA256SUMS
```

Install the wheel into a dedicated environment as your normal user. For
example, for release `0.1.0`:

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python medion_erazer_major_x10_ec-0.1.0-py3-none-any.whl
.venv/bin/medion-fan-control --demo
```

Checksums detect damaged or mismatched downloads; they are not an independent
signature. Review the release source and hardware safety requirements before
running the launcher with administrator privileges.
