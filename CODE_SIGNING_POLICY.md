# Code Signing Policy

## Current status

VMSG release binaries are currently **unsigned**. Windows may therefore show an
Unknown Publisher or Microsoft Defender SmartScreen warning on first run, and
macOS Gatekeeper will object similarly.

Verify downloads against the SHA-256 checksum published alongside each release
asset:

```bash
sha256sum -c vmsg-linux-amd64.sha256
```
```powershell
Get-FileHash vmsg-windows-amd64.exe -Algorithm SHA256
```

Every release asset also ships a `.build-info.json` recording the VMSG version,
build timestamp, Python version, platform, artifact SHA-256, and the exact
dependency versions the binary was built from.

If you would rather not trust a downloaded binary at all, build from source —
the same command CI runs:

```bash
pip install -r requirements-release.txt
python build_binary.py
```

## Project roles

- Committers and reviewers: [@Cyclotronic](https://github.com/Cyclotronic)
- Release and signing approver: [@Cyclotronic](https://github.com/Cyclotronic)

Changes from contributors require maintainer review. The maintainer is also the
trusted author for direct maintenance changes. If additional maintainers join,
this policy will name their roles before they can approve releases.

## Release provenance

Official binaries are built from tagged source by the repository's GitHub
Actions workflows, never uploaded from a developer machine.

The pipeline is deliberately split so that an unverified artifact cannot reach a
release page:

1. `.github/workflows/ci.yml` — static analysis, offline protocol fidelity, and
   integration tests on Windows and Linux, for every push and pull request.
2. `.github/workflows/build-binaries.yml` — builds from the tracked `vmsg.spec`
   with pinned dependencies from `requirements-release.txt`, then runs
   `tools/verify_frozen_build.py` against the packaged executable. Read-only
   permissions; it can produce artifacts but cannot publish them.
3. `.github/workflows/publish-release.yml` — runs only for `v*` tags, checks the
   tag matches `vmsg_core/version.py`, rebuilds and re-verifies from the tagged
   source, and only then publishes. This is the only workflow granted
   `contents: write`.

`tools/verify_frozen_build.py` exists because passing tests against the source
tree says nothing about the bundle. It launches the built executable and checks
that the dashboard was actually packaged, that API authentication is enforced,
that the Prologix socket answers, and that modules reached only through
function-level imports survived freezing. Those failures are otherwise silent:
the process starts, most things work, and one feature is quietly dead.

## Future signing

Should VMSG apply to and be accepted by a free open-source signing program such
as the SignPath Foundation, this page will be updated to state the arrangement
and name the verified publisher. No application has been made and no
certificate has been granted. Until this section says otherwise, treat any
"signed" VMSG binary as not originating from this project.

## Reporting

Report a suspected tampered or malicious artifact through the process in
[SECURITY.md](SECURITY.md), not as a public issue.
