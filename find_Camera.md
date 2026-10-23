
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

Finding an IP Camera on Linux
Known camera details
Camera IP: 192.168.1.242
Camera MAC: 00:00:00:00:09:01
Network interface: wlp58s0
1. Find network interfaces and IP addresses
ip -br addr

2. Capture ARP traffic
sudo tcpdump -i wlp58s0 -nn -e arp


Look for ARP packets containing the camera's IP or MAC address.

3. Scan for devices
sudo arp-scan --interface=wlp58s0 --localnet


Note: This scans the local subnet and might not discover devices on a different IP subnet.

4. Configure access to the camera subnet

Add a route to the camera's subnet:

sudo ip route add 192.168.1.0/24 dev wlp58s0 scope link


Add an IP address on that subnet if necessary. First, check existing addresses:

ip -4 addr show dev wlp58s0
ip route

If 192.168.1.10/32 exists, remove it and add a /24 address:

sudo ip addr del 192.168.1.10/32 dev wlp58s0
sudo ip addr add 192.168.1.10/24 dev wlp58s0


Only do this if 192.168.1.10 is available and not already in use.

5. Verify camera connectivity

Send ARP requests:

sudo arping -I wlp58s0 192.168.1.242


Check the selected route and source IP:

ip route get 192.168.1.242


Expected output should include:

192.168.1.242 dev wlp58s0 src 192.168.1.10


If necessary, configure a specific route:

sudo ip route replace 192.168.1.242/32 dev wlp58s0 src 192.168.1.10


Troubleshooting notes
arping success means a device responds to ARP; it does not guarantee that HTTP or ping is supported.
ping can fail when ICMP is disabled.
A static IP on another subnet may require an additional address and route.
A switch normally does not forward all unicast packets to every port. Use managed-switch port mirroring if you need to capture another device's unicast traffic.
Wi-Fi client isolation or VLAN separation can prevent communication even when devices are physically connected to the same switch.
The IP addresses and routes configured with ip are temporary and usually disappear after reboot.
Quick workflow
ip -br addr — identify the interface and addresses.
tcpdump — observe network traffic.
arping — confirm the device responds.
Configure the correct subnet address and route.
nmap — identify available services.
Open the appropriate URL in the browser.
