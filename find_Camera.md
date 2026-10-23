
sudo tcpdump -i wlp58s0 -e


sudo arping -I wlp58s0 192.168.1.242
ping 192.168.1.242
ip -br addr
ip route
sudo ip route add 192.168.1.0/24 dev wlp58s0 scope link
ip route

sudo arping -I wlp58s0 192.168.1.242 # Works
ip route get 192.168.1.242
sudo ip route replace 192.168.1.242/32 dev wlp58s0 src 192.168.1.10
