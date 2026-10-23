FROM python:3.15-slim AS build

WORKDIR /app

#COPY docker/install_build.sh /app/
#RUN sh install_build.sh

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install uv
# renovate: datasource=pypi depName=uv
ARG UV_VERSION=0.13.4
RUN python -m pip install --no-cache-dir uv==${UV_VERSION}

# Build the virtualenv where the runtime stage expects it, against the image's
# own interpreter rather than a uv-managed download.
ENV VIRTUAL_ENV=/app/venv
ENV UV_PROJECT_ENVIRONMENT=$VIRTUAL_ENV
ENV UV_PYTHON_DOWNLOADS=never
ENV UV_LINK_MODE=copy
ENV PATH=$VIRTUAL_ENV/bin:$PATH

# Install python dependencies (but not self)
COPY pyproject.toml uv.lock logging.yaml /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Copy self, set the version & install. The step above already checked the
# lock, and stamping the version makes pyproject.toml disagree with it, so
# this sync is --frozen rather than --locked.
COPY shm shm
ARG APP_VERSION="0.0.0"
RUN uv version --frozen "$APP_VERSION"
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.15-slim AS shm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=off

# Passing the -d /app will set that as the home dir & chown it
RUN useradd -r -U -m -d /app shm

WORKDIR /app

#COPY --chown=shm:shm docker/install_runtime.sh /app/
#RUN sh install_runtime.sh

COPY --chown=shm:shm --from=build /app /app

ENV VIRTUAL_ENV=/app/venv
ENV PATH=$VIRTUAL_ENV/bin:$PATH

USER shm

CMD ["uvicorn", "--log-config", "logging.yaml", "--host", "0.0.0.0", "--port", "9000", "shm.main:app"]
