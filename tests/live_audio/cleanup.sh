#!/bin/bash
# destroy every falcon_test_* null sink this harness created
for id in $(pw-dump 2>/dev/null | python3 -c "
import sys,json
for o in json.load(sys.stdin):
    if str(o.get('info',{}).get('props',{}).get('node.name','')).startswith('falcon_test'): print(o['id'])"); do pw-cli destroy $id >/dev/null 2>&1; done
