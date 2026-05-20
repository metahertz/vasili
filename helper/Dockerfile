# vasili-helper — single-image bundle of the server-side counterparts of vasili.
#
# Bundles: DNS proxy, PMKID crack server, iodine, WireGuard, sshd-on-53,
# and a small Flask UI to configure them. Process supervision via supervisord.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HELPER_CONFIG_DIR=/etc/vasili-helper \
    HELPER_STATE_DIR=/var/lib/vasili-helper

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-flask \
        iodine \
        openssh-server \
        wireguard-tools \
        hashcat \
        supervisor \
        iptables iproute2 \
        dnsutils ca-certificates \
        curl jq \
        sqlite3 \
        openssl \
    && rm -rf /var/lib/apt/lists/*

# Copy the existing server-side scripts verbatim from the repo.
COPY server/vasili-crack-server.py /opt/vasili/server/vasili-crack-server.py
COPY server/vasili-dns-proxy.py    /opt/vasili/server/vasili-dns-proxy.py

# Helper UI + supervised launchers
COPY helper/app/         /opt/vasili-helper/app/
COPY helper/launchers/   /opt/vasili-helper/launchers/
COPY helper/entrypoint.sh /entrypoint.sh
COPY helper/supervisord.conf /etc/supervisor/conf.d/vasili-helper.conf

RUN chmod +x /entrypoint.sh /opt/vasili-helper/launchers/*.sh \
 && mkdir -p "$HELPER_CONFIG_DIR" "$HELPER_STATE_DIR" /var/log/vasili-helper \
 && ssh-keygen -A

EXPOSE 53/udp 53/tcp 8080/tcp

VOLUME ["/etc/vasili-helper", "/var/lib/vasili-helper"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
