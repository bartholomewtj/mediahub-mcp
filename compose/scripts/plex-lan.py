#!/usr/bin/env python3
"""Make Plex treat LAN clients as local and advertise the Windows LAN address.

Docker Desktop has no bridged networking, so every client arrives from the
Docker gateway and Plex would treat them all as remote (remote bitrate caps,
forced transcodes). Two Preferences.xml attributes fix that:

  LanNetworks        subnets treated as local (home LAN + Docker gateways)
  customConnections  URL advertised to clients so they direct-connect over LAN

LanNetworks is a hidden preference (not settable via /:/prefs), so this edits
Preferences.xml in the plex volume. Run through plex-lan.ps1, which stops Plex
first and starts it after. No token needed.

  python plex-lan.py --prefs "/config/Library/Application Support/Plex Media Server/Preferences.xml" --host-ip <host-lan-ip>
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET

LAN_NETWORKS = "192.168.0.0/16,172.16.0.0/12,10.0.0.0/8"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefs", required=True)
    ap.add_argument("--host-ip", required=True, help="Windows LAN address, e.g. 192.168.1.50")
    ap.add_argument("--lan", default=LAN_NETWORKS)
    args = ap.parse_args()

    tree = ET.parse(args.prefs)
    root = tree.getroot()
    want = {
        "LanNetworks": args.lan,
        "customConnections": f"http://{args.host_ip}:32400",
    }
    changed = False
    for k, v in want.items():
        if root.attrib.get(k) != v:
            print(f"{k}: {root.attrib.get(k)!r} -> {v!r}")
            root.attrib[k] = v
            changed = True
        else:
            print(f"{k}: already {v!r}")
    if changed:
        tree.write(args.prefs, encoding="utf-8", xml_declaration=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
