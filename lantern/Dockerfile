FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/lantern/ ./lantern/
COPY src/web/ ./web/
# Example config: copied into the app's /config folder on first start (never overwrites your edits)
COPY src/config/ ./default_config/

ENV LANTERN_PORT=8099
EXPOSE 8099

CMD ["python", "-m", "lantern.main"]
