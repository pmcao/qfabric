#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
#
# Author: Komal Thareja (kthare10@renci.org)
#
# Deploy and run QFabric BB84 locally using network namespaces (alice, bob)
# and a local BMv2 switch inside a single Linux VM.
#
# Usage:
#     sudo python3 scripts/deploy_local_vm.py [--scenario validation/scenarios/baseline_1km.yml]
#     sudo python3 scripts/deploy_local_vm.py --netem-experiment
#     sudo python3 scripts/deploy_local_vm.py --sweep-experiment

from __future__ import annotations

import builtins
_original_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault("flush", True)
    _original_print(*args, **kwargs)

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from qne.config import ScenarioConfig
from validation.scenario import ValidationScenario
from validation.compare import ValidationResult, load_fabric_result

# Network Namespace Configuration
NS_ALICE = "alice"
NS_BOB = "bob"

IFACE_ALICE = "veth_alice"
IFACE_BOB = "veth_bob"
IFACE_SW_ALICE = "veth_sw_a"
IFACE_SW_BOB = "veth_sw_b"

MAC_ALICE = "02:00:00:00:00:01"
MAC_SW_ALICE = "02:00:00:00:00:0a"
MAC_SW_BOB = "02:00:00:00:00:0b"
MAC_BOB = "02:00:00:00:00:02"

ALICE_IP = "10.10.1.1"
BOB_IP = "10.10.1.2"
CLASSICAL_PORT = 5100


def run_cmd(cmd: str | list[str], shell=True, check=True, quiet=False) -> tuple[str, str]:
    """Execute a command locally in the root namespace."""
    if not quiet:
        print(f"  Executing: {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
    
    proc = subprocess.run(
        cmd,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(PROJECT_DIR)
    )
    
    if check and proc.returncode != 0:
        print(f"  ERROR: Command failed (code {proc.returncode})")
        print(f"  STDOUT: {proc.stdout}")
        print(f"  STDERR: {proc.stderr}")
        raise RuntimeError(f"Command failed: {cmd}")
        
    return proc.stdout.strip(), proc.stderr.strip()


def run_in_ns(ns: str, cmd: str, check=True, quiet=False) -> tuple[str, str]:
    """Execute a command inside a specific network namespace."""
    ns_cmd = f"sudo ip netns exec {ns} {cmd}"
    return run_cmd(ns_cmd, shell=True, check=check, quiet=quiet)


def check_root():
    """Ensure running as root to manipulate network namespaces and interfaces."""
    if os.getuid() != 0:
        print("ERROR: This script must be run as root (using sudo) to configure network interfaces and namespaces.")
        sys.exit(1)


def ensure_network_setup():
    """Run setup_local_netns.sh if the interfaces or namespaces are missing."""
    if not Path(f"/var/run/netns/{NS_ALICE}").exists() or not Path(f"/var/run/netns/{NS_BOB}").exists():
        print("\n=== Initializing local network namespaces ===")
        run_cmd(f"bash {PROJECT_DIR}/scripts/setup_local_netns.sh", quiet=False)


def setup_sim_envs(netsquid_user=None, netsquid_pass=None):
    """Build simulator virtualenvs locally on the VM (idempotent)."""
    netsquid_user = netsquid_user or os.environ.get("NETSQUID_USER")
    netsquid_pass = netsquid_pass or os.environ.get("NETSQUID_PASS")

    print("\n=== Setting up local simulator envs (one-time setup, several minutes) ===")

    print("  Setting up QFabric-sim environment (.venv-qsim)...")
    run_cmd(
        "python3 -m venv .venv-qsim && "
        ".venv-qsim/bin/pip install -q --upgrade pip && "
        ".venv-qsim/bin/pip install -q numpy pyyaml",
        quiet=False
    )

    print("  Setting up SeQUeNCe 1.0 environment (.venv-seq, requires python3.12)...")
    # Vagrantfile provisions python3.12
    python_exe = "python3.12" if os.system("which python3.12 >/dev/null 2>&1") == 0 else "python3"
    run_cmd(
        f"{python_exe} -m venv .venv-seq && "
        ".venv-seq/bin/pip install -q --upgrade pip && "
        ".venv-seq/bin/pip install -q numpy pyyaml 'sequence==1.0.0'",
        quiet=False
    )

    print("  Setting up NetSquid environment (.venv-nsq)...")
    creds = ""
    if netsquid_user and netsquid_pass:
        creds = f"--extra-index-url 'https://{netsquid_user}:{netsquid_pass}@pypi.netsquid.org'"
    else:
        print("    WARNING: NETSQUID_USER/NETSQUID_PASS not set — NetSquid will not install.")
    
    run_cmd(
        "python3 -m venv .venv-nsq && "
        ".venv-nsq/bin/pip install -q --upgrade pip && "
        ".venv-nsq/bin/pip install -q numpy pyyaml",
        quiet=False
    )
    if creds:
        run_cmd(f".venv-nsq/bin/pip install -q {creds} netsquid", quiet=False)

    print("  Simulator environments ready.")


def configure_switch(threshold: int):
    """Compile P4 program and start BMv2 locally."""
    print(f"\n=== Configuring switch (threshold={threshold}) ===")

    # Stop any previous switch run
    run_cmd("sudo docker rm -f bmv2 2>/dev/null || true", quiet=True)
    run_cmd("sudo pkill -f simple_switch 2>/dev/null || true; sleep 1", quiet=True)

    json_rel = "p4/bmv2/quantum_channel.json"
    image = os.environ.get("QFABRIC_BMV2_IMAGE", "").strip()

    if image:
        print(f"  Using BMv2 Docker image: {image}")
        print("  Compiling P4 (in container)...")
        run_cmd(
            f"sudo docker run --rm -v {PROJECT_DIR}:/work {image} "
            f"p4c-bm2-ss --std p4-16 -o /work/{json_rel} "
            f"-I /work/p4/bmv2/includes /work/p4/bmv2/quantum_channel.p4",
            quiet=True
        )
        print("  Starting BMv2 in Docker container...")
        run_cmd(
            f"sudo docker run -d --name bmv2 --privileged --network host "
            f"-v {PROJECT_DIR}:/work {image} "
            f"simple_switch --interface 0@{IFACE_SW_ALICE} --interface 1@{IFACE_SW_BOB} "
            f"--log-level warn /work/{json_rel}",
            quiet=True
        )
        time.sleep(3)
        cli = "sudo docker exec -i bmv2 simple_switch_CLI"
    else:
        print("  Compiling P4 locally...")
        run_cmd(
            f"p4c-bm2-ss --std p4-16 -o {json_rel} -I p4/bmv2/includes "
            f"p4/bmv2/quantum_channel.p4",
            quiet=True
        )
        print("  Starting BMv2 simple_switch locally...")
        run_cmd(
            f"sudo simple_switch --interface 0@{IFACE_SW_ALICE} --interface 1@{IFACE_SW_BOB} "
            f"--log-level warn {PROJECT_DIR}/{json_rel} > /tmp/bmv2.log 2>&1 &",
            quiet=True
        )
        time.sleep(2)
        cli = "simple_switch_CLI"

    # Hex MAC conversions for tables
    alice_mac_hex = "0x" + MAC_ALICE.replace(":", "")
    sw_alice_mac_hex = "0x" + MAC_SW_ALICE.replace(":", "")
    sw_bob_mac_hex = "0x" + MAC_SW_BOB.replace(":", "")
    bob_mac_hex = "0x" + MAC_BOB.replace(":", "")

    print("  Configuring switch tables...")
    for cmd in (
        f"table_add quantum_channel_params set_channel_params 0 => {threshold} 1 {sw_bob_mac_hex} {bob_mac_hex}",
        f"table_add port_forwarding port_forward 0 => 1 {sw_bob_mac_hex} {bob_mac_hex}",
        f"table_add port_forwarding port_forward 1 => 0 {sw_alice_mac_hex} {alice_mac_hex}",
    ):
        run_cmd(f'echo "{cmd}" | {cli} --thrift-port 9090', quiet=True)

    print("  Switch configured and running")


def setup_dataplane_ips():
    """Assign data-plane IPs and static ARP entries inside namespaces."""
    print("\n=== Setting up data-plane IPs ===")

    print(f"  Alice Namespace: {ALICE_IP}/24 on {IFACE_ALICE}")
    run_in_ns(
        NS_ALICE,
        f"ip addr flush dev {IFACE_ALICE} && "
        f"ip addr add {ALICE_IP}/24 dev {IFACE_ALICE} && "
        f"ip link set {IFACE_ALICE} up",
        quiet=True
    )

    print(f"  Bob Namespace:   {BOB_IP}/24 on {IFACE_BOB}")
    run_in_ns(
        NS_BOB,
        f"ip addr flush dev {IFACE_BOB} && "
        f"ip addr add {BOB_IP}/24 dev {IFACE_BOB} && "
        f"ip link set {IFACE_BOB} up",
        quiet=True
    )

    print("  Configuring static ARP entries...")
    # Alice sends to Switch Alice-side MAC
    run_in_ns(
        NS_ALICE,
        f"ip neigh replace {BOB_IP} lladdr {MAC_SW_ALICE} nud permanent dev {IFACE_ALICE}",
        quiet=True
    )
    # Bob sends to Switch Bob-side MAC
    run_in_ns(
        NS_BOB,
        f"ip neigh replace {ALICE_IP} lladdr {MAC_SW_BOB} nud permanent dev {IFACE_BOB}",
        quiet=True
    )

    # Promiscuous mode for raw sockets
    run_in_ns(NS_ALICE, f"ip link set {IFACE_ALICE} promisc on", quiet=True)
    run_in_ns(NS_BOB, f"ip link set {IFACE_BOB} promisc on", quiet=True)

    # Verify connectivity via ping
    print("  Testing connectivity (ping from Alice NS to Bob NS)...")
    stdout, _ = run_in_ns(NS_ALICE, f"ping -c 3 -W 2 {BOB_IP}", check=False, quiet=True)
    if "0 received" in stdout or "100% packet loss" in stdout:
        print(f"  WARNING: Ping failed! Switch forwarding tables or BMv2 might be down.")
        print(f"  Ping output: {stdout}")
    else:
        print("  Ping successful!")


def run_bb84(scenario_path: str):
    """Run BB84 protocol: start Bob CLI in namespace 'bob', Alice CLI in 'alice'."""
    print("\n=== Running BB84 protocol ===")

    # Ensure venv + local results directory exist
    results_dir = PROJECT_DIR / "results"
    results_dir.mkdir(exist_ok=True)

    # Copy the scenario configuration into a shared location for nodes
    shared_scenario = PROJECT_DIR / "scenario.yml"
    shared_scenario.write_text(Path(PROJECT_DIR / scenario_path).read_text())

    print("  Cleaning up previous runs...")
    run_in_ns(NS_BOB, "pkill -f 'qne.cli' 2>/dev/null || true", check=False, quiet=True)
    run_in_ns(NS_ALICE, "pkill -f 'qne.cli' 2>/dev/null || true", check=False, quiet=True)
    run_cmd(f"rm -f {results_dir}/*.json /tmp/bob.log /tmp/alice.log", quiet=True)

    venv_python = f"{PROJECT_DIR}/.venv/bin/python3"
    if not Path(venv_python).exists():
        print(f"  Creating local venv...")
        run_cmd("python3 -m venv .venv && .venv/bin/pip install -q pyyaml numpy", quiet=False)

    print("  Starting Bob (receiver)...")
    # Start Bob CLI in Bob's namespace
    bob_cmd = (
        f"sudo ip netns exec {NS_BOB} {venv_python} -m qne.cli bob "
        f"--config scenario.yml "
        f"--interface {IFACE_BOB} "
        f"--host '0.0.0.0' "
        f"--output results/bob_results.json "
        f"> /tmp/bob.log 2>&1 &"
    )
    subprocess.Popen(bob_cmd, shell=True, cwd=str(PROJECT_DIR))
    time.sleep(3)  # Allow Bob to boot and listen

    print("  Starting Alice (sender)...")
    # Start Alice CLI in Alice's namespace
    alice_cmd = (
        f"sudo ip netns exec {NS_ALICE} {venv_python} -m qne.cli alice "
        f"--config scenario.yml "
        f"--interface {IFACE_ALICE} "
        f"--bob-host '{BOB_IP}' "
        f"--dst-mac '{MAC_SW_ALICE}' --src-mac '{MAC_ALICE}' "
        f"--output results/alice_results.json "
        f"> /tmp/alice.log 2>&1"
    )
    
    # Alice runs synchronously in the foreground
    run_cmd(alice_cmd, shell=True, quiet=False)
    print("  Alice finished.")
    time.sleep(2)  # Give Bob time to finalize writing output

    # Re-align filenames to match FABRIC output expectations
    if (results_dir / "bob_results.json").exists():
        (results_dir / "fabric_bob_results.json").write_text((results_dir / "bob_results.json").read_text())
    if (results_dir / "alice_results.json").exists():
        (results_dir / "fabric_alice_results.json").write_text((results_dir / "alice_results.json").read_text())

    # Print BB84 results summary
    bob_path = results_dir / "fabric_bob_results.json"
    if bob_path.exists():
        try:
            bob_results = json.loads(bob_path.read_text())
            print("\n=== Local VM Emulation BB84 Results ===")
            for key in ["photons_sent", "photons_received", "sifted_bits",
                         "qber", "secure_key_rate", "final_key_bits", "elapsed_seconds"]:
                print(f"  {key}: {bob_results.get(key, 'N/A')}")
        except json.JSONDecodeError:
            print("\n  WARNING: Bob results are invalid JSON. Check logs.")
    else:
        print("\n  WARNING: No Bob results found! Run may have failed. Logs in /tmp/bob.log")


def apply_classical_netem(delay_ms=0, jitter_ms=0, loss_pct=0.0,
                          alice_delay_ms=None, bob_delay_ms=None):
    """Impair ONLY the classical sifting TCP channel (port 5100) inside namespaces."""
    pairs = [
        (NS_ALICE, IFACE_ALICE, delay_ms if alice_delay_ms is None else alice_delay_ms),
        (NS_BOB, IFACE_BOB, delay_ms if bob_delay_ms is None else bob_delay_ms),
    ]
    print(f"\n=== Applying classical impairment (TCP:{CLASSICAL_PORT}) ===")
    for ns, iface, d in pairs:
        netem = "netem"
        if d:
            netem += f" delay {d}ms" + (f" {jitter_ms}ms" if jitter_ms else "")
        if loss_pct:
            netem += f" loss {loss_pct}%"

        run_in_ns(
            ns,
            f"tc qdisc del dev {iface} root 2>/dev/null || true; "
            f"tc qdisc add dev {iface} root handle 1: prio && "
            f"tc qdisc add dev {iface} parent 1:3 handle 30: {netem} && "
            f"tc filter add dev {iface} protocol ip parent 1: prio 1 u32 match ip dport {CLASSICAL_PORT} 0xffff flowid 1:3 && "
            f"tc filter add dev {iface} protocol ip parent 1: prio 1 u32 match ip sport {CLASSICAL_PORT} 0xffff flowid 1:3",
            quiet=True
        )


def clear_classical_netem():
    """Clear traffic control rules from namespaces."""
    print("  Clearing classical netem...")
    for ns, iface in [(NS_ALICE, IFACE_ALICE), (NS_BOB, IFACE_BOB)]:
        run_in_ns(ns, f"tc qdisc del dev {iface} root 2>/dev/null || true", quiet=True)


def run_cross_validation_local(scenario_path: str) -> list[ValidationResult]:
    """Run local cross-validation: QFabric (measured) + simulators."""
    print("\n=== Running Local Cross-Validation ===")
    results_dir = PROJECT_DIR / "results"
    results = []

    # 1. QFabric Emulated Result
    qf = load_fabric_result(results_dir / "fabric_bob_results.json")
    if qf is not None:
        qf.platform = "qfabric"
        results.append(qf)
        print(f"  QFabric (VM Measured): QBER={qf.qber:.4f}, sifted={qf.sifted_bits}")

    # 2. QFabric-sim (Python-only simulator)
    print("  QFabric-sim...")
    stdout, _ = run_cmd(f".venv-qsim/bin/python3 -m validation.run_qfabric {scenario_path} --json -", quiet=True)
    results.append(parse_adapter_stdout(stdout, "qfabric_sim", scenario_path))

    # 3. SeQUeNCe
    if Path(".venv-seq/bin/python3").exists():
        print("  SeQUeNCe...")
        stdout, _ = run_cmd(f".venv-seq/bin/python3 -m validation.run_sequence {scenario_path} --json -", quiet=True)
        results.append(parse_adapter_stdout(stdout, "sequence", scenario_path))
    
    # 4. NetSquid
    if Path(".venv-nsq/bin/python3").exists():
        print("  NetSquid...")
        stdout, _ = run_cmd(f".venv-nsq/bin/python3 -m validation.run_netsquid {scenario_path} --json -", quiet=True)
        results.append(parse_adapter_stdout(stdout, "netsquid", scenario_path))

    return [r for r in results if r]


def parse_adapter_stdout(stdout: str, platform: str, scenario_path: str) -> ValidationResult | None:
    """Helper to parse sentinel-wrapped validation results."""
    sentinel = "___QFABRIC_RESULT___"
    for line in stdout.splitlines():
        if line.startswith(sentinel):
            payload = json.loads(line[len(sentinel):])
            res = ValidationResult(
                platform=platform,
                scenario_name=payload.get("scenario_name"),
                photons_sent=payload.get("photons_sent", 0),
                photons_received=payload.get("photons_received", 0),
                sifted_bits=payload.get("sifted_bits", 0),
                qber=payload.get("qber", 0.0),
                raw_key_rate=payload.get("raw_key_rate", 0.0),
                secure_key_rate=payload.get("secure_key_rate", 0.0),
                extra=payload.get("extra", {})
            )
            print(f"    {platform.capitalize()}: QBER={res.qber:.4f}, sifted={res.sifted_bits}")
            return res
    print(f"  WARNING: Failed to parse result for platform {platform}")
    return None


def run_network_conditions_experiment(scenario_path: str):
    """Run classical impairment sweep and record results."""
    conditions = [
        {"name": "baseline"},
        {"name": "latency_25ms", "delay_ms": 25},
        {"name": "latency_100ms", "delay_ms": 100},
        {"name": "jitter_50pm20ms", "delay_ms": 50, "jitter_ms": 20},
        {"name": "loss_1pct", "loss_pct": 1.0},
        {"name": "asymmetric_100_10ms", "alice_delay_ms": 100, "bob_delay_ms": 10},
    ]

    results_dir = PROJECT_DIR / "results"
    rows = []

    try:
        for i, cond in enumerate(conditions, 1):
            name = cond.get("name")
            print(f"\n##### [{i}/{len(conditions)}] Classical condition: {name} #####")
            clear_classical_netem()
            
            netem_kw = {k: v for k, v in cond.items() if k != "name"}
            if netem_kw:
                apply_classical_netem(**netem_kw)

            # Re-run BB84
            try:
                run_bb84(scenario_path)
            except Exception as e:
                print(f"  !! run failed under {name}: {e}")

            row = {"condition": name, **netem_kw}
            bob_path = results_dir / "fabric_bob_results.json"
            if bob_path.exists():
                try:
                    d = json.loads(bob_path.read_text())
                    elapsed = d.get("elapsed_seconds", 0.0) or 0.0
                    fk = d.get("final_key_bits", 0)
                    row.update({
                        "qber": d.get("qber", 0.0),
                        "sifted_bits": d.get("sifted_bits", 0),
                        "final_key_bits": fk,
                        "secure_key_rate": d.get("secure_key_rate", 0.0),
                        "elapsed_seconds": elapsed,
                        "key_bits_per_sec": (fk / elapsed) if elapsed > 0 else 0.0,
                    })
                except json.JSONDecodeError:
                    row["error"] = "invalid results json"
            else:
                row["error"] = "no result (run failed/timed out)"
            
            print(f"  Result: QBER={row.get('qber')}, elapsed={row.get('elapsed_seconds')}s, "
                  f"bits/s={row.get('key_bits_per_sec')}")
            rows.append(row)
            (results_dir / "network_effects.json").write_text(json.dumps(rows, indent=2))
    finally:
        clear_classical_netem()

    print(f"\nSaved local VM network effects sweep -> {results_dir / 'network_effects.json'}")


def run_all_scenarios_sweep(scenarios_dir="validation/scenarios", cross_validate=True):
    """Run all scenarios in the scenarios directory (like notebook 05)."""
    results_dir = PROJECT_DIR / "results"
    tmp_dir = results_dir / "_tmp_scenarios"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    sdir = PROJECT_DIR / scenarios_dir

    points = []
    for f in sorted(glob.glob(str(sdir / "*.yml"))):
        group = Path(f).stem
        if "sweep" in group:
            points += [(s, group) for s in ValidationScenario.load_sweep(f)]
        else:
            points.append((ValidationScenario.from_yaml(f), group))
    
    print(f"Discovered {len(points)} sweep scenario runs.")

    rows = []
    for i, (vs, group) in enumerate(points, 1):
        print(f"\n##### [{i}/{len(points)}] {group} : {vs.name} (dist={vs.distance_km} km) #####")
        
        cfg_dict = {
            "name": vs.name,
            "channel": {"distance_km": vs.distance_km,
                        "attenuation_db_per_km": vs.attenuation_db_per_km,
                        "polarization_fidelity": vs.polarization_fidelity},
            "detector": {"efficiency": vs.detector_efficiency,
                         "dark_count_rate": vs.dark_count_rate_hz},
            "protocol": {"num_photons": vs.num_photons,
                         "sample_fraction": vs.sample_fraction,
                         "send_rate_hz": 10000.0},
            "seed": vs.seed,
        }
        safe_name = f"{group}__{vs.name}".replace("=", "-").replace(".", "p").replace("/", "-")
        tmp_path = tmp_dir / f"{safe_name}.yml"
        tmp_path.write_text(yaml_dump_flat(cfg_dict))
        rel = str(tmp_path.relative_to(PROJECT_DIR))

        (results_dir / "fabric_bob_results.json").unlink(missing_ok=True)

        config = ScenarioConfig.from_dict(cfg_dict)
        try:
            configure_switch(config.loss_threshold_u32)
            run_bb84(rel)
            if cross_validate:
                backends = run_cross_validation_local(rel)
            else:
                m = load_fabric_result(results_dir / "fabric_bob_results.json")
                backends = [m] if m else []
        except Exception as e:
            print(f"  !! Point failed: {e}")
            backends = []

        rows.append({
            "group": group,
            "scenario": vs.name,
            "distance_km": vs.distance_km,
            "attenuation_db_per_km": vs.attenuation_db_per_km,
            "polarization_fidelity": vs.polarization_fidelity,
            "backends": [b.to_payload() for b in backends if b],
        })
        (results_dir / "all_scenarios.json").write_text(json.dumps(rows, indent=2))

    print(f"\nSaved all sweep results -> {results_dir / 'all_scenarios.json'}")


def yaml_dump_flat(d: dict) -> str:
    """Simple YAML dump helper avoiding pyyaml dependency on main script path if not in venv."""
    import yaml
    return yaml.safe_dump(d)


def main():
    check_root()
    ensure_network_setup()

    parser = argparse.ArgumentParser(description="Deploy QFabric BB84 Emulation on Local VM Namespaces")
    parser.add_argument(
        "--scenario", default="validation/scenarios/baseline_1km.yml",
        help="Scenario YAML config file",
    )
    parser.add_argument("--skip-install", action="store_true", help="Skip building simulator environments")
    parser.add_argument("--netem-experiment", action="store_true", help="Run the classical netem conditions sweep")
    parser.add_argument("--sweep-experiment", action="store_true", help="Run all scenarios sweep validation")
    args = parser.parse_args()

    # Load configuration
    config = ScenarioConfig.from_yaml(PROJECT_DIR / args.scenario)
    threshold = config.loss_threshold_u32
    print(f"\nScenario: {config.name}")
    print(f"  Distance: {config.channel.distance_km} km")
    print(f"  Loss probability: {config.loss_probability:.4f}")
    print(f"  P4 threshold: {threshold}")

    if not args.skip_install:
        setup_sim_envs()

    if args.netem_experiment:
        configure_switch(threshold)
        setup_dataplane_ips()
        run_network_conditions_experiment(args.scenario)
    elif args.sweep_experiment:
        run_all_scenarios_sweep()
    else:
        # Standard Single Run
        configure_switch(threshold)
        setup_dataplane_ips()
        run_bb84(args.scenario)
        run_cross_validation_local(args.scenario)


if __name__ == "__main__":
    main()
