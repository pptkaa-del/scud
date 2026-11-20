sudo sed -i '/extended/d' /etc/apt/sources.list;
sudo apt update;
sudo apt install -y docker.io docker-compose-v2;
sudo usermod -aG docker "$(whoami)";
exec su - "$(whoami)";
