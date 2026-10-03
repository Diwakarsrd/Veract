# Image used when policy.json has {"sandbox": {"mode": "docker"}}.
# Build once:  docker build -t agent-runtime-sandbox sandbox/
# Add your project's test dependencies below; the container has no network at run time.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==8.* && useradd -m -u 10001 runner
USER runner
WORKDIR /ws
