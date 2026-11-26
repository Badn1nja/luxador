FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY luxador/ ./luxador/
COPY web/ ./web/
COPY config/ ./default_config/

ENV LUXADOR_PORT=8099
EXPOSE 8099

CMD ["python", "-m", "luxador.main"]
