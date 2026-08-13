# Contributing to VMSG

Thanks for looking. VMSG is a VISA-to-Prologix/LXI gateway that sits between
software such as TestController and real instruments, so the bar for changes is
"does this still behave correctly against hardware", not just "do the tests
pass".

## Before you open a PR

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt

python -m pyflakes vmsg.py vmsg_core tests tools   # must be silent
python tools/verify_offline.py                     # no hardware needed
```

`verify_offline.py` starts its own gateway on a scratch port with mock
instruments and a temporary config. It never touches a running gateway or your
`mappings.json`, so it is safe to run on a live bench.

The integration suites need a running gateway:

```bash
python vmsg.py &
python tests/test_prologix_gateway.py
python tests/test_query_atomicity_and_config.py
```

They snapshot the gateway configuration and restore it afterwards. If a run is
interrupted, check your mappings before trusting the next result.

CI runs all of the above on Windows and Linux.

## Things worth knowing

**Lock ordering.** Anything that touches a shared bus takes the *interface* lock
outer and the *resource* lock inner. Reversing this deadlocks the Prologix
listener against the VXI-11 path. See `prologix_server.py` and
`vxi11_bridge.py`.

**Don't bind with `reuse_address`.** On Windows that permits two live sockets on
one port, so a second instance silently steals traffic. Use
`netutil.start_exclusive_server` / `create_tcp_listener`, which fail loudly
instead.

**Silence is the enemy.** A cooldown, a dropped device, or a truncated buffer
that produces no log line turns a five-minute diagnosis into an evening. If you
add a failure path, make it say so.

**The control API is authenticated.** New endpoints are covered automatically by
the middleware in `apiauth.py`. If you add a genuinely public endpoint, add it to
`PUBLIC_PATHS` deliberately and say why.

## Benchmarks

`tools/benchmark.py` gives comparable numbers across sessions. Quote a
before/after when a change is meant to affect performance:

```bash
python tools/benchmark.py --test all --addresses 1,2,3
```

## Building and releasing

```bash
pip install -r requirements-release.txt   # pinned toolchain
python build_binary.py                    # checks -> build -> verify
```

`build_binary.py` runs pyflakes and `verify_offline.py` *before* building,
packages from the tracked `vmsg.spec`, writes provenance and a checksum into
`dist/`, then runs `tools/verify_frozen_build.py` against the executable it just
produced.

That last step matters more than it looks. Passing tests against the source tree
says nothing about the bundle: PyInstaller resolves imports statically, and
`--add-data` mappings can silently not apply. Either failure yields a process
that starts fine with one feature quietly dead — most often the dashboard, which
is bundled wholesale from `static/`.

If you add a module that is imported inside a function rather than at module
scope, add it to `hiddenimports` in `vmsg.spec` and make sure the frozen check
actually exercises it.

Releases are cut by tagging `vX.Y.Z`. The publish workflow refuses to run if the
tag does not match `vmsg_core/version.py`, rebuilds from the tagged source, and
publishes only after verification passes. Nothing is uploaded from a developer
machine.

## Style

Match the surrounding code. Comments should explain *why* something is done, not
restate the line beneath them. Docstrings on modules and non-obvious functions.

## Reporting bugs

Include the VMSG version, OS, VISA backend, what the client was (TestController
version, PyVISA, other), and the relevant log lines at DEBUG. For protocol
issues, a wire trace from both sides is worth more than a description.

Security issues go through [SECURITY.md](SECURITY.md), not a public issue.
