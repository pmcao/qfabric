# QFabric: Local Virtual Machine Setup Guide

This guide details how to run the QFabric quantum network emulation platform locally inside a Linux Virtual Machine (VM) instead of provisioning a slice on the remote FABRIC cloud testbed.

---

## Why a Linux VM?

The QFabric live emulation requires:
1. **Raw Ethernet sockets (`AF_PACKET`)** to transmit custom `0x7101` EtherType photon packets. These require Linux kernel-level socket support and root (`sudo`) privileges.
2. **Traffic Control (`tc` and `netem`)** to apply network impairments specifically to the classical TCP/IP sifting channel.
3. **BMv2 P4 software switch** (`simple_switch`) attached to network interfaces.

Running inside a Linux Virtual Machine is the most portable and robust way to run these features on non-Linux host operating systems (such as macOS or Windows).

---

## Emulation Architecture (Single VM + Network Namespaces)

Instead of provisioning three separate virtual machines (Alice, Switch, and Bob) which consumes heavy CPU and memory resources, we emulate the entire topology inside a **single Linux VM** using **Linux Network Namespaces (`ip netns`)**:

```
                 [ VM Root / Switch Namespace ]
                   simple_switch (BMv2)
                ┌────────────────────────┐
                │ Port 0: veth_sw_a      │
                │ Port 1: veth_sw_b      │
                └────────┬──────┬────────┘
                         │      │
     ┌───────────────────┘      └───────────────────┐
     │ veth_sw_a                                    │ veth_sw_b
     │ (MAC: 02:00:00:00:00:0a)                     │ (MAC: 02:00:00:00:00:0b)
     │                                              │
     │ veth_alice                                   │ veth_bob
     │ (MAC: 02:00:00:00:00:01)                     │ (MAC: 02:00:00:00:00:02)
┌────┴───────────────────────┐                 ┌────┴───────────────────────┐
│     [ Alice Namespace ]    │                 │      [ Bob Namespace ]     │
│   qne.cli alice (10.10.1.1)│                 │   qne.cli bob (10.10.1.2)  │
└────────────────────────────┘                 └────────────────────────────┘
```

- **Alice Node**: Runs in the `alice` namespace, sending photons over `veth_alice` (IP: `10.10.1.1`).
- **Bob Node**: Runs in the `bob` namespace, receiving photons and running the classical server on `veth_bob` (IP: `10.10.1.2`).
- **Switch Node**: Runs in the root/default VM namespace, running `simple_switch` attached to `veth_sw_a` and `veth_sw_b`.
- **Impairments**: Applied using `tc netem` rules directly on `veth_alice` and `veth_bob` inside their namespaces, isolating classical TCP port 5100 sifting traffic from photon packets.

---

## Created Configuration & Scripts

We have added the following files to support local VM emulation:

1. **`Vagrantfile`**:
   Provisions a single Ubuntu 22.04 VM, allocates 4GB RAM, 2 CPUs, forwards port `8888` (for Jupyter Notebooks), mounts the workspace, and pre-installs dependencies (Docker, pip, venv, python3.12, tcpdump).

2. **`scripts/setup_local_netns.sh`**:
   A shell script to initialize network namespaces, create virtual ethernet pairs, assign static MAC addresses, and disable IPv6 (to avoid L2 network chatter).

3. **`scripts/deploy_local_vm.py`**:
   A python deployment orchestrator that mimics `deploy_fabric.py` but targets local namespaces and executes commands locally. It handles:
   - Compiling P4 and starting `simple_switch` (supports local or Docker-based BMv2).
   - Setting IP addresses, promisc mode, and static ARP tables.
   - Bootstrapping Python venvs for validation simulators (SeQUeNCe and NetSquid).
   - Running the BB84 sifting exchange.
   - Performing traffic control impairment sweeps (`--netem-experiment`).
   - Running all scenario sweeps (`--sweep-experiment`).

---

## Step-by-Step Instructions

### Step 1: Spin up the Virtual Machine
From your host terminal (macOS/Windows) in the project root:
```bash
vagrant up
```
This downloads the base box (if not already cached) and provisions all systems.

### Step 2: SSH into the VM
```bash
vagrant ssh
```
This logs you into the VM bash shell. The QFabric directory is synced to `/home/vagrant/qfabric`. Move there:
```bash
cd /home/vagrant/qfabric
```

### Step 3: Run the Emulation
You must run the deployment script as root (`sudo`) to manage interfaces and namespaces:

* **Single Run (Baseline Emulation & Simulator Cross-Validation)**:
  ```bash
  sudo python3 scripts/deploy_local_vm.py
  ```
  *This compiles P4, brings up the switch and namespaces, runs BB84, and runs cross-validation.*

* **Classical Network Impairment Sweep (tc netem)**:
  ```bash
  sudo python3 scripts/deploy_local_vm.py --netem-experiment
  ```
  *Measures the throughput/elapsed time of BB84 sifting under baseline, latency, jitter, and packet loss conditions.*

* **All Scenarios Sweep**:
  ```bash
  sudo python3 scripts/deploy_local_vm.py --sweep-experiment
  ```
  *Runs the entire suite of scenario YAML configurations (distances, loss thresholds) and compares results.*

---

## Running Jupyter Notebooks in the VM

To run the interactive notebooks locally inside the VM:
1. SSH into the VM: `vagrant ssh`
2. Start Jupyter Notebook inside the VM, binding to all interfaces:
   ```bash
   jupyter notebook --ip=0.0.0.0 --no-browser
   ```
3. Open the URL printed by Jupyter in your host machine's browser. Since Vagrant forwards port `8888`, it will load seamlessly.
