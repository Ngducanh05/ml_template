FROM python:3.12.14-slim-trixie@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MLSERVER_PARALLEL_WORKERS=0 MLSERVER_HTTP_PORT=8080 MLSERVER_GRPC_PORT=9000 MLSERVER_MODEL_IMPLEMENTATION=mlserver_mlflow.MLflowRuntime MLFLOW_ENABLE_TELEMETRY=false MLFLOW_TRACKING_URI=sqlite:////tmp/mlflow.db HOME=/tmp
WORKDIR /opt/runtime
COPY requirements.txt /opt/runtime/requirements.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: -r /opt/runtime/requirements.txt \
    && python -m pip check \
    && python -m pip freeze --all > /opt/runtime/resolved-requirements.txt \
    && python -m pip uninstall -y pip \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app
COPY request.json /opt/runtime/technical-contract.json
USER 10001:10001
EXPOSE 8080 9000
CMD ["mlserver", "start", "/mnt/models"]
