FROM caddy:2.11.4

# 由面板流水线构建参数注入：
#   --build-arg UPSTREAM=192.168.0.104:6016   ASR 语音上游
#   --build-arg LISTEN=8111                   容器内监听端口（=流水线「对外端口」）
ARG UPSTREAM=192.168.0.104:6016
ARG LISTEN=8111

COPY Caddyfile.template /etc/caddy/Caddyfile
RUN sed -i "s|__UPSTREAM__|${UPSTREAM}|g; s|__LISTEN__|${LISTEN}|g" /etc/caddy/Caddyfile

EXPOSE 8111
