FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

EXPOSE 8765
VOLUME ["/data"]
CMD ["cvbench-studio", "serve", "--host", "0.0.0.0", "--data-dir", "/data"]
