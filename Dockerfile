FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# コピー元と起動対象を hems.py に変更
COPY hems.py .

RUN mkdir -p /app/data

ENV HEMS_IP=192.168.0.146

CMD ["python", "hems.py"]
