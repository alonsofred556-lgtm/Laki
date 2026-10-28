FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY signer/requirements.txt /app/signer/requirements.txt
RUN pip install --no-cache-dir -r /app/signer/requirements.txt
COPY signer /app/signer

USER 65532:65532
EXPOSE 8090
CMD ["uvicorn", "signer.app:app", "--host", "0.0.0.0", "--port", "8090"]
