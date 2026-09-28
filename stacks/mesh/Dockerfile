# The Meshtastic Python CLI, containerised so provisioning the node never
# installs anything on the host. Rebuilt rarely; pin the CLI so a rebuild
# after a wipe reproduces the version the runbook was tested with.
FROM python:3.12-slim
RUN pip install --no-cache-dir meshtastic==2.7.11
ENTRYPOINT ["python", "-m", "meshtastic"]
