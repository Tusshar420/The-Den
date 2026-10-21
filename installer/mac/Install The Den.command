#!/bin/bash
# Double-click this file to install The Den. It runs install.sh next to it in a Terminal window.
cd "$(dirname "$0")" || exit 1
/bin/bash ./install.sh "$@"
status=$?
echo
read -r -p "Press Return to close this window. " _
exit $status
