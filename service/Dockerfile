# Build from repo root:  docker build -f service/Dockerfile -t factbench-service .
FROM python:3.12-slim
WORKDIR /srv
COPY scorer/ /srv/scorer/
COPY tasks/ /srv/tasks/
COPY service/server.py /srv/service/server.py
EXPOSE 8830
CMD ["python3", "/srv/service/server.py"]
