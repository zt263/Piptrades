FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements-pip.txt ./requirements-pip.txt
RUN pip install --no-cache-dir -r requirements-pip.txt
COPY src ./src
COPY pip_app.py ./pip_app.py
COPY static ./static
CMD ["python", "pip_app.py"]
