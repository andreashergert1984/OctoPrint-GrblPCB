# Development image: OctoPrint from source (the plugin needs the
# ConnectedPrinter API of OctoPrint 2.x) with this plugin installed editable.
FROM python:3.13-slim

ARG OCTOPRINT_REF=dev

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "OctoPrint @ git+https://github.com/OctoPrint/OctoPrint.git@${OCTOPRINT_REF}"

# Editable install: the compose file mounts the working copy over /plugin,
# so code changes only need a restart of the container
COPY . /plugin
RUN pip install --no-cache-dir -e /plugin

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# OctoPrint refuses to run as root; dialout gives access to passed-through
# serial devices
RUN useradd --create-home --groups dialout octoprint \
    && mkdir /data && chown octoprint:octoprint /data
USER octoprint

EXPOSE 5000
VOLUME /data
ENTRYPOINT ["/entrypoint.sh"]
