#!/bin/bash
# Double-click this file to remove The Den from this Mac. It runs uninstall.sh next to it in a Terminal window.
cd "$(dirname "$0")" || exit 1
/bin/bash ./uninstall.sh "$@"
status=$?
echo
read -r -p "Press Return to close this window. " _
exit $status
