# One image, three loggers -- docker-compose.yml picks the subcommand.
FROM python:3.12-slim

# meshtastic pulls pyserial + pypubsub (the pub/sub bus radiolog.py subscribes
# to). paho 2.x only: the script uses the VERSION2 callback API.
RUN pip install --no-cache-dir \
    "meshtastic>=2.3,<3" \
    "paho-mqtt>=2,<3" \
    "requests>=2,<3"

WORKDIR /app
COPY radiolog.py .

# -u so prints hit docker logs immediately, not on buffer flush.
ENTRYPOINT ["python", "-u", "radiolog.py"]
