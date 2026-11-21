# Mech Royale bot (Builders Buildathon). The match runs it with no network, a read-only root,
# 1 CPU, 512 MB, uid 65534, and the map at /arena.
FROM python:3.13.5-slim-bookworm
WORKDIR /bot
COPY bot.py arena.py expo.bin ./
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
