# vasili

## Just one ping

Vasili is designed to get you online "no matter what" in a fully automated way; by enumerating, testing and then picking the best connectivity from your surrounding options at any given time.

### Motivation

I do a lot of travel for work, and i'm constantly finding myself offline due to crappy captive portals, timeouts, and just generally bad wifi.

I like the idea of a portable "router" which will just try it's best to get and maintain a connection, which you can power on when you arrive at a new hotel and thats it. Maybe you even have other items in your travel kit (spotify speker / alexa etc) which are configured to always use vasili for their internet connectivity, as well as your phone/laptop while in the room.

### Design

There are lots of different types of Wifi, open, wep/wpa1-3, captive portals of various functionality and security. Vasili is designed to have modular plugins for each possible scenario, which can be enabled and disabled by the user.

In a "map/reduce" style, I want all available networks to be presented to the modules, which accept whichever networks they are compatible with and return a success/failure if connectivity is available.

For example, only open networks would be accepted by a captive portal login module, as passing a WPA3 protected network to that module would be pointless.

There will also be speedtest module which then runs for each sucessful connection found, so that ultimately the best connection can be chosen.

As a hobbyist pentester, there will also be "offensive" modules, such as attempting of DNS/ICMP tunneling, and maybe table-based WPA brute force of common SSID's. These are to be used only on your own networks or with the permission of the network owners and included for the acedemic challenge rather than for use. These modules will be disabled by default and likley require additional configuration by the end user.

### Hardware

The intended platform for this is a Rasberry PI 4, with multiple USB Wifi cards.

Multiple cards allows for modules to run simulaniously to find a working connection quicker.

Current thinking (may change with implimentation):

- One card always scanning for networks, storing metadata for the other modules in a local DB/Data structure.

- A pool of cards (at least 1) that are used by the modules to test each candidate network

- A "Currently chosen" card, which provides the actual internet connection to the end user, through iptables NAT.

### Contributing

If you think this is useful, feel free to reach out! Would be great to build this with others :)

If you already know of a tool like this and i'm waisting my time, please also let me know!
