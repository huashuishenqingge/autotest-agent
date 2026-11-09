FROM python:3.12.10-slim
RUN python -m pip install --no-cache-dir pytest==8.3.5 iniconfig==2.0.0 packaging==24.2 pluggy==1.5.0 coverage==7.8.0
COPY container_worker.py /opt/autotest/container_worker.py
COPY pytest.ini /opt/autotest/pytest.ini
USER 65534:65534
WORKDIR /workspace
