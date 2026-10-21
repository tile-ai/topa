ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root

# The TileOPs runner already carries the libisl build required by isl-python.
# Keep it ahead of Ubuntu's older ABI-compatible soname.
ENV LD_LIBRARY_PATH=/usr/local/lib:${LD_LIBRARY_PATH}

COPY entrypoint.sh /usr/local/bin/topa-entrypoint
RUN chmod 0755 /usr/local/bin/topa-entrypoint

CMD ["/usr/local/bin/topa-entrypoint"]
