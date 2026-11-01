FROM caddy:2.11.4

ARG LISTEN=8111
ARG ENDPOINT_ROLE=asr
ENV LISTEN=${LISTEN} ENDPOINT_ROLE=${ENDPOINT_ROLE}

COPY Caddyfile.template /etc/caddy/Caddyfile.template
COPY entrypoint.sh /usr/local/bin/capswriter-gateway
RUN chmod +x /usr/local/bin/capswriter-gateway
ENTRYPOINT ["/usr/local/bin/capswriter-gateway"]

EXPOSE 8111
