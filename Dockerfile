FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir fastapi>=0.111 pydantic>=2.7 redis>=5.0 httpx>=0.27 opentelemetry-api>=1.25 opentelemetry-sdk>=1.25 uvicorn
COPY . .
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
