# Single-driver lease

`DriverLease` is a host-only guard around one call that drives an existing
Run.  The lock is rooted at the Run directory, not at a segment, so execute,
resume, and future drive entry points cannot overlap across segments.

```python
from modport.runner import DriverLease, read_driver_health

with DriverLease(run_dir, run_id, heartbeat_interval=10) as lease:
    while drive_one_tick():
        lease.check_health()  # raises promptly if heartbeat persistence failed

snapshot = read_driver_health(run_dir)
# snapshot["status"] is running, stopped, or failed
```

The lease only owns exclusion and health publication.  It does not recover or
retry attempts, renew a budget, or settle an SDK effect.  In particular, an
effect whose outcome is unknown stays pending until an explicit coordination
decision is made.

Health is atomically published at
`artifacts/monitor/monitor-driver.json`.  Its `run_id`, `run_dir`, `pid`, and
`birth` fields match the independent run monitor's driver identity.  Readers
must use both `pid` and `birth` when deciding whether a process is still the
same driver, and should choose their own freshness threshold for `timestamp`.

## Optional service policy

An operating-system service may use `Restart=on-failure`, but it should also
set both a restart interval and a finite burst limit.  The drive command uses
exit `0` for SDK success, `78` for handled non-success states, and `1` for an
unhandled host failure.  SDK terminal failure or cancellation, an open wait,
`recovery_required`, and an unknown effect are coordination outcomes rather
than service crashes.  Exit `78` must therefore prevent automatic restart.

For systemd, the relevant policy shape is:

```ini
[Unit]
StartLimitIntervalSec=15min
StartLimitBurst=3

[Service]
Type=exec
ExecStart=/opt/modport/bin/modport drive --run-dir=/srv/modport/run --run-id=RUN_ID
Restart=on-failure
RestartPreventExitStatus=78
RestartSec=30s
```

This is guidance only.  Substitute deployment-specific paths and identity,
and do not install a service until the drive command and coordination policy
are configured for that deployment.

The desktop supervisor implements this exit policy with a three-start burst
limit over fifteen minutes. Before execution admission, the driver uses the
public SDK read-only work-availability inspector to short-circuit terminal
Runs. Its marker records the authoritative business state separately from
process health, the terminal reason, and semantic/process exit codes. An
ordinary nonterminal return without an open coordination wait exits `1`.

Windows Task Scheduler restarts any nonzero exit without a code exclusion.
Only at the desktop scheduler entrypoint, handled exit `78` becomes process
exit `0`; the SDK outcome and semantic exit `78` remain recorded. Native
Windows startup and recovery acceptance remain unverified. Neither platform's
on-failure policy detects a process that unexpectedly exits with code `0`.

Desktop registration binds the logical instance to its directory. Supported
continuation may replace the current frozen header with an SDK successor
segment in that directory. SDK inspection and resume use the header's current
`run_id`; service identity, credentials, UI and messages retain the registered
desktop instance ID. This does not relax frozen deployment admission or add
compatibility for historical Runs.

Until a successor publishes its own projection, desktop status reads its SDK
lifecycle state only. Predecessor terminal state, task rows and acceptance
cannot be relabelled as successor evidence. Terminal driver entry also settles
pending advisory messages. The public read-only availability API exposes task
identity/state but omits reply payloads: a proven undispatched queued message
is marked unexecuted, while a missing dispatched result remains explicitly
unverified. Neither case dispatches another assignment or writes SDK storage.
