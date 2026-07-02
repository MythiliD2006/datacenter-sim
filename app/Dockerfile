FROM python:3.11-slim

WORKDIR /app

# install dependencies first — separate layer so Docker caches it
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy app source code
COPY . .

# create logs directory — persisted via docker-compose volume mount
RUN mkdir -p logs

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]