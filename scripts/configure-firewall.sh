#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo 'Administrator authorization required.' >&2; exit 1; fi
# Add only the ARK UDP ports. Do not reset rules or alter the default policy.
/usr/sbin/ufw status verbose
/usr/sbin/ufw allow 7777:7778/udp comment 'ARK Genesis game'
/usr/sbin/ufw allow 27015/udp comment 'ARK Genesis query'
/usr/sbin/ufw status verbose
