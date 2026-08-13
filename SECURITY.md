# Security Policy

## Supported versions

Security fixes are made against the current default branch and included in the
next release. Once stable releases exist, only the latest stable release will
receive security fixes unless a release notice states otherwise.

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue. Use the
repository's **Report a vulnerability** form:

https://github.com/Cyclotronic/VMSG/security/advisories/new

Include the affected version, operating system, reproduction steps, impact,
and any proposed mitigation. Do not include credentials, private instrument
captures, or unrelated personal information.

The maintainer will acknowledge a complete report, assess its severity, and
coordinate a fix and disclosure when warranted. This is a best-effort
open-source project and does not promise a particular response time.

## What VMSG exposes by design

VMSG is a gateway. It intentionally opens network listeners so that clients such
as TestController can reach instruments through it:

| Port | Purpose |
| :--- | :--- |
| 1234 | Prologix-compatible control socket |
| 5025 | LXI raw SCPI socket |
| 111 / 1024 | VXI-11 portmap and core channel |
| 8080 | Web dashboard and control API |
| 5353/udp | mDNS discovery advertisement |

These listeners bind `0.0.0.0` so that instruments can be reached from other
hosts. An expected listener or mDNS advertisement is not by itself a
vulnerability.

**The control API on 8080 requires a token.** It can change instrument mappings,
send arbitrary SCPI to physical hardware, and stop or restart the gateway, so
authentication is the boundary that matters. A token is generated on first run
and stored in the configuration file; `VMSG_API_TOKEN` overrides it.

Please do report: authentication bypass on the control API, a way to reach a
state-changing endpoint without a valid token, cross-origin access that the CORS
policy should have refused, or arbitrary code execution.

## Deployment guidance

VMSG has no transport encryption and its token is a bearer credential. Treat it
as a device on a trusted instrument network:

- Do not expose port 8080 to the public Internet or forward it through a router.
- Prefer a segregated lab VLAN or a host firewall restricting who can reach the
  listeners.
- `api_auth_enabled: false` exists for isolated single-user benches. Setting it
  leaves the control API fully open to anyone who can reach the port.
- Rotate the token by deleting `api_token` from the config and restarting, or by
  setting `VMSG_API_TOKEN`.
