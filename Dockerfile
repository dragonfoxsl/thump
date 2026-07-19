FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --target=/deps .

FROM python:3.12-slim
COPY --from=build /deps /deps
ENV PYTHONPATH=/deps
ENV THUMP_CONFIG=/etc/thump/config.yaml
RUN useradd -r -u 10001 thump && mkdir -p /var/lib/thump && chown thump /var/lib/thump
USER thump
EXPOSE 8080
CMD ["python", "-m", "thump.main"]
