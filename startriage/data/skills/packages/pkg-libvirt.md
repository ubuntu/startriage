---
package: libvirt
---
## validate-report

"Permission denied" problems with libvirt are often caused by the AppArmor
profiles libvirt applies to guests. If the report has no AppArmor denials, the
verdict is usually `incomplete`: ask for the output of
`sudo dmesg | grep -i apparmor` (or `journalctl -k | grep -i apparmor`) taken
right after reproducing the problem.
