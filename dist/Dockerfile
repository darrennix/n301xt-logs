FROM caddy:2-alpine

WORKDIR /srv
COPY . /srv

EXPOSE 8080
CMD ["caddy", "run", "--config", "/srv/Caddyfile", "--adapter", "caddyfile"]
