#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
#
# Author: Komal Thareja (kthare10@renci.org)
#
# Set up local network namespaces (alice, bob) and veth interfaces to emulate
# the QFabric topology on a single VM. Must be run with sudo.

set -euo pipefail

# Ensure running as root
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please run this script with sudo."
    exit 1
fi

NS_ALICE="alice"
NS_BOB="bob"

# Fixed MAC addresses for deterministic setup
MAC_ALICE="02:00:00:00:00:01"
MAC_SW_ALICE="02:00:00:00:00:0a"
MAC_SW_BOB="02:00:00:00:00:0b"
MAC_BOB="02:00:00:00:00:02"

echo "=== Cleaning up existing network namespaces and veth pairs ==="
ip netns del "$NS_ALICE" 2>/dev/null || true
ip netns del "$NS_BOB" 2>/dev/null || true
ip link del veth_sw_a 2>/dev/null || true
ip link del veth_sw_b 2>/dev/null || true

echo "=== Creating network namespaces ==="
ip netns add "$NS_ALICE"
ip netns add "$NS_BOB"

echo "=== Creating veth interfaces with fixed MACs ==="
# Alice <-> Switch (port 0)
ip link add veth_alice address "$MAC_ALICE" type veth peer name veth_sw_a address "$MAC_SW_ALICE"
# Switch (port 1) <-> Bob
ip link add veth_bob address "$MAC_BOB" type veth peer name veth_sw_b address "$MAC_SW_BOB"

echo "=== Moving interfaces to namespaces ==="
ip link set veth_alice netns "$NS_ALICE"
ip link set veth_bob netns "$NS_BOB"

echo "=== Configuring interfaces in namespace '$NS_ALICE' ==="
ip netns exec "$NS_ALICE" ip link set lo up
ip netns exec "$NS_ALICE" ip link set veth_alice up
# Disable IPv6 to prevent neighbor discovery traffic from polluting the raw sockets
ip netns exec "$NS_ALICE" sysctl -w net.ipv6.conf.veth_alice.disable_ipv6=1 >/dev/null

echo "=== Configuring interfaces in namespace '$NS_BOB' ==="
ip netns exec "$NS_BOB" ip link set lo up
ip netns exec "$NS_BOB" ip link set veth_bob up
ip netns exec "$NS_BOB" sysctl -w net.ipv6.conf.veth_bob.disable_ipv6=1 >/dev/null

echo "=== Configuring interfaces in root (switch) namespace ==="
ip link set veth_sw_a up
ip link set veth_sw_b up
sysctl -w net.ipv6.conf.veth_sw_a.disable_ipv6=1 >/dev/null
sysctl -w net.ipv6.conf.veth_sw_b.disable_ipv6=1 >/dev/null

echo "=== Network Setup Complete ==="
echo "Topology:"
echo "  [Alice NS] veth_alice ($MAC_ALICE) <---> veth_sw_a ($MAC_SW_ALICE) [Root NS / Switch]"
echo "  [Bob NS]   veth_bob   ($MAC_BOB)   <---> veth_sw_b ($MAC_SW_BOB)   [Root NS / Switch]"
echo
echo "To run QFabric local emulation, run 'python3 scripts/deploy_local_vm.py'"
