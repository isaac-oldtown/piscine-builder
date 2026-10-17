#!/bin/bash
count=0
if [ "$1" = "-l" ]; then
    while IFS= read -r line; do
        count=$((count + 1))
    done
else
    while read -r -a w || [ "${#w[@]}" -gt 0 ]; do
        count=$((count + ${#w[@]}))
        w=()
    done
fi
echo "$count"
