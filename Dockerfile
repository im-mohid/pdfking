FROM python:3.12-slim
# LibreOffice powers Word/Excel/PowerPoint -> PDF tools (tools auto-hide if it is missing)
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer libreoffice-calc libreoffice-impress fonts-dejavu-core fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m app
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY .en[v] ./
USER app
ENV PYTHONUNBUFFERED=1
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips="*"
