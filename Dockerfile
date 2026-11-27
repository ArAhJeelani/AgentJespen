FROM python:3.12-slim
WORKDIR /service
COPY pyproject.toml alembic.ini ./
COPY app app
COPY agent agent
COPY alembic alembic
COPY tests tests
RUN pip install --no-cache-dir '.[test]'
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0"]
