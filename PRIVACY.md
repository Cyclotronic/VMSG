# Privacy

VMSG does not transmit telemetry, usage analytics, crash reports, or personal
information. It does not contact an update service or any VMSG-operated Internet
service.

If the gateway hits an unhandled failure, it writes a diagnostic traceback under
the platform log directory (`%LOCALAPPDATA%\VMSG\logs` on Windows) and prints the
path. That file stays on the local computer and is never uploaded. You decide
whether to inspect, delete, or share it when reporting a problem.

VMSG is an instrument gateway. When it runs it may:

- listen on the configured ports (Prologix 1234, LXI raw 5025, VXI-11 111/1024,
  web dashboard 8080);
- answer protocol requests from clients that connect to those listeners;
- advertise itself on the local network using mDNS when discovery is enabled; and
- open VISA sessions to the instruments named in your mappings.

These operations provide the gateway's function. No resulting traffic is sent to
the VMSG maintainers or to a third-party analytics service.

Instrument traffic and the log buffer remain in application memory unless you
explicitly export them. Your configuration, including instrument mappings and
the API token, is stored locally in `mappings.json`.

Note that `mappings.json` contains the control-API token. Treat it as a
credential: do not commit it, and scrub it before sharing a configuration file
when reporting a problem.

Questions about this policy may be opened as a GitHub issue that contains no
sensitive information.
