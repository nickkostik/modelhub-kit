# Optional time synchronization after resume

If a Linux machine using `systemd-timesyncd` resumes with a stale clock, this
optional unit asks the existing time service to reconnect after sleep or
hibernation. It does not start a time service that was disabled and does not
run continuously. The connection still requires a reachable time server.

Check that `systemd-timesyncd` is active, then install the example from a
reviewed checkout. The first command refuses to overwrite an existing unit.

```sh
systemctl is-active systemd-timesyncd.service
sudo sh -c 'set -C; umask 022; cat > /etc/systemd/system/modelhub-timesync-resume.service' < examples/systemd/modelhub-timesync-resume.service
sudo systemd-analyze verify /etc/systemd/system/modelhub-timesync-resume.service
sudo systemctl daemon-reload
sudo systemctl enable modelhub-timesync-resume.service
```

Check it is enabled with `systemctl is-enabled modelhub-timesync-resume.service`.
The next normal resume exercises the hook; there is no need to interrupt active
sessions to test it. If the clock remains wrong, inspect the time service and
network reachability. This example does not modify the time server selection.
