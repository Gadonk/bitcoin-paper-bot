FROM python:3.12-slim
ARG APP_VERSION=development
ARG VCS_REF=unknown
ARG SOURCE_URL=https://github.com/Gadonk/bitcoin-paper-bot
LABEL org.opencontainers.image.source=$SOURCE_URL \
      org.opencontainers.image.version=$APP_VERSION \
      org.opencontainers.image.revision=$VCS_REF \
      org.opencontainers.image.description="Bitcoin AI paper-trading bot; simulated money only"
ENV APP_VERSION=$APP_VERSION
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 WEB_PORT=8080
WORKDIR /app
COPY bot.py dashboard.py strategy.py ai_review.py ai_settings.py ai_smoke.py backtest.py config.json config-v2.json config-v3.json ./
COPY web/ ./web/
COPY tests/ ./tests/
RUN python -m unittest discover -s tests -v
USER 99:100
EXPOSE 8080
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "/app/bot.py"]
CMD ["paper", "--config", "/config/config.json", "--state", "/data/paper.json"]
