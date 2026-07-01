# -*- mode: ruby -*-
# vi: set ft=ruby :

Vagrant.configure("2") do |config|
  config.vm.box = "ubuntu/jammy64"
  config.vm.hostname = "qfabric-local-vm"

  # Forward port for Jupyter notebooks
  config.vm.network "forwarded_port", guest: 8888, host: 8888, auto_correct: true

  # Sync the QFabric workspace directory
  config.vm.synced_folder ".", "/home/vagrant/qfabric", type: "virtualbox"

  # VirtualBox Provider Settings
  config.vm.provider "virtualbox" do |vb|
    vb.memory = "4096"
    vb.cpus = 2
    # Ensure network interfaces can go into promiscuous mode for raw sockets
    vb.customize ["modifyvm", :id, "--nicpromisc2", "allow-all"]
    vb.customize ["modifyvm", :id, "--nicpromisc3", "allow-all"]
  end

  # Libvirt Provider Settings (for Apple Silicon or Linux hosts using KVM)
  config.vm.provider "libvirt" do |lv|
    lv.memory = 4096
    lv.cpus = 2
  end

  # Provisioning: Install Python 3.12 (via deadsnakes PPA for SeQUeNCe 1.0),
  # pip, venv, Docker, and other network tools.
  config.vm.provision "shell", inline: <<-SHELL
    echo "=== Running VM provisioning ==="
    export DEBIAN_FRONTEND=noninteractive
    
    # Update and install system dependencies
    apt-get update -qq
    apt-get install -y -qq \
      software-properties-common \
      build-essential \
      python3-pip \
      python3-venv \
      python3-dev \
      git \
      tcpdump \
      iproute2 \
      docker.io

    # Add deadsnakes PPA for Python 3.12 (needed for SeQUeNCe 1.0 validation)
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y -qq python3.12 python3.12-venv python3.12-dev

    # Enable and configure Docker
    systemctl enable --now docker
    usermod -aG docker vagrant

    echo "=== Provisioning complete ==="
    echo "To log in, run: vagrant ssh"
    echo "To set up the network namespaces inside the VM, run: bash scripts/setup_local_netns.sh"
  SHELL
end
