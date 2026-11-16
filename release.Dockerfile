FROM gcr.io/distroless/static-debian12:nonroot
ARG TARGETARCH
ARG VERSION
ARG COMMIT
LABEL org.opencontainers.image.source="https://github.com/ebenderooock/loom" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.title="Loom" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}"
USER nonroot:nonroot
WORKDIR /
COPY loom-${TARGETARCH} /usr/local/bin/loom
EXPOSE 1925
VOLUME ["/config", "/media"]
ENV LOOM_HTTP_ADDR=:1925 LOOM_CONFIG_DIR=/config LOOM_DATA_DIR=/config
ENTRYPOINT ["/usr/local/bin/loom"]
CMD ["serve"]
