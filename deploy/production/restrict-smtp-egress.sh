#!/bin/sh
# Keep only the approved SMTP port 465 available if the provider opens its SMTP group.
set -eu
for chain in OUTPUT DOCKER-USER; do
    if ! iptables -w -C "$chain" -p tcp -m conntrack --ctstate NEW -m multiport --dports 25,587,2525 -j REJECT --reject-with tcp-reset 2>/dev/null; then
        iptables -w -I "$chain" 1 -p tcp -m conntrack --ctstate NEW -m multiport --dports 25,587,2525 -j REJECT --reject-with tcp-reset
    fi
done
if ! ip6tables -w -C OUTPUT -p tcp -m conntrack --ctstate NEW -m multiport --dports 25,587,2525 -j REJECT --reject-with tcp-reset 2>/dev/null; then
    ip6tables -w -I OUTPUT 1 -p tcp -m conntrack --ctstate NEW -m multiport --dports 25,587,2525 -j REJECT --reject-with tcp-reset
fi
