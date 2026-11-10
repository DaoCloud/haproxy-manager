FROM debian:trixie-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 openssl \
    && apt-get clean
COPY tools/integration/backend.py tools/integration/lab.py /lab/
ENTRYPOINT ["/usr/bin/python3", "/lab/backend.py"]
CMD []
