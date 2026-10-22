# Desktop indexing and memory pressure

Check which process and cgroup are stalled before changing maintenance limits.
Available host RAM does not exclude a local cgroup memory throttle. On cgroup v2,
`memory.high` can force reclaim and stall allocations even when `memory.max`
has not been reached. Inspect both the affected unit and its ancestors. See the
[kernel memory controller documentation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#memory).

For a KDE desktop with Baloo installed, run these read-only commands as the
desktop user:

```sh
systemctl --user show kde-baloo.service \
  -p ActiveState -p MainPID -p ControlGroup -p MemoryCurrent -p MemoryHigh -p MemoryMax
balooctl6 config list hidden
balooctl6 config list contentIndexing
balooctl6 config list includeFolders
balooctl6 config list excludeFolders
```

Use the returned `ControlGroup` to inspect that group's `memory.current`,
`memory.high`, `memory.events.local` and `memory.pressure` under
`/sys/fs/cgroup`. Compare bounded samples: cumulative high events alone do not
establish a current stall. Inspect the relevant parent groups too. An inactive
or unavailable Baloo unit does not establish that Baloo caused the slowdown.

If Baloo is indexing disposable downloads or rapidly changing generated state,
add only the intended directories to its native exclusions. For example, when
using the default kit state location:

```sh
balooctl6 config add excludeFolders "$HOME/.local/state/modelhub-kit"
balooctl6 config list excludeFolders
```

Run the add command only if that directory exists and is not already excluded.
KDE's [configuration command source](https://github.com/KDE/baloo/blob/master/src/tools/balooctl/configcommand.cpp)
checks existing directories and rejects duplicate entries. Use the actual state
location if `XDG_STATE_HOME` is customized. Review other generated directories
individually; do not exclude a whole home directory or replace the user's
existing include/exclude lists. Leave content and hidden-file preferences alone.

Where the running Baloo version exposes its existing D-Bus service, a bounded
configuration refresh can be requested without starting another indexer:

```sh
timeout 5s busctl --user --auto-start=no call \
  org.kde.baloo / org.kde.baloo.main updateConfig
```

A saved exclusion and a successful runtime refresh are separate evidence. A
timeout leaves the persisted configuration available for the next normal
indexer start; do not label the running process refreshed. Confirm subsequent
pressure and indexing activity before declaring the issue resolved. Do not
restart an active application, purge its index, or delete files to force a
passing check.

Raising a memory threshold is not a routine cleanup policy. If a verified
indexer cannot process its new exclusions because it is already throttled,
any temporary runtime-only change needs a recorded prior value, available host
capacity, bounded observation and an explicit restoration check. Do not lower
the threshold back below current usage and recreate the stall, or silently
leave an unverified temporary setting. This procedure does not change kit
maintenance controls or limit interactive harnesses, and installs no watcher,
additional timer, backup, or growing diagnostic log.
