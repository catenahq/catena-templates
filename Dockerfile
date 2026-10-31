# Serves the generated catalog over HTTP: a local preview of a catalog
# change before it merges.
#
#   docker build -t catena-templates:local .
#   docker run --rm -p 8080:8080 catena-templates:local
#   # then open http://<host>:8080/ (index.html) or
#   # http://<host>:8080/catalog.json
#
# The generated files and the blueprints folders are copied in: sources/ is
# a build input and is not served.

FROM nginxinc/nginx-unprivileged:1.31-alpine

COPY templates.json /usr/share/nginx/html/templates.json
COPY catalog.json /usr/share/nginx/html/catalog.json
COPY index.html /usr/share/nginx/html/index.html
COPY blueprints /usr/share/nginx/html/blueprints

# The server answers any origin, so a page on another address can fetch the
# catalog files. The server block is written as root, and nginx serves it
# as uid 101.
USER root
RUN printf '%s\n' \
    'server {' \
    '  listen 8080;' \
    '  root /usr/share/nginx/html;' \
    '  add_header Access-Control-Allow-Origin "*";' \
    '  location / { autoindex on; }' \
    '}' > /etc/nginx/conf.d/default.conf
USER 101

EXPOSE 8080
