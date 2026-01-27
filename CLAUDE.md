# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Smart Home Prometheus Metrics (shm) is a Python-based FastAPI service that collects metrics from smart home devices and exposes them in Prometheus format. It integrates with:
- **SmartThings**: Samsung SmartThings devices (sensors, switches, thermostats)
- **Ecobee**: Ecobee thermostats and sensors
- **WeatherAPI**: Weather data

The service runs as a containerized application designed for Kubernetes deployment with Prometheus scraping.

## Development Commands

### Environment Setup
```bash
# Install dependencies with Poetry
poetry install

# Run the service locally
poetry run uvicorn --log-config logging.yaml --host 0.0.0.0 --port 9000 shm.main:app
```

### Code Quality
```bash
# Format code (runs isort + black)
make format

# Run all checks (isort, black, pylint, mypy)
make check

# Individual checks
make check/isort   # Import sorting
make check/black   # Code formatting
make check/pylint  # Linting
make check/mypy    # Type checking
```

### Testing
```bash
# Run tests with XML output (for CI)
make test

# Run tests with HTML coverage report
make cov
```

### Clean
```bash
# Remove build artifacts and cache directories
make clean
```

### Docker
```bash
# Build Docker image
docker build -t shm:dev --build-arg APP_VERSION=0.0.0 .

# Run container
docker run -p 9000:9000 shm:dev
```

### Helm
```bash
# Package chart
helm package charts/smart-home-metrics -u -d charts --version <VERSION> --app-version <APP_VERSION>

# Install/upgrade locally
helm upgrade --install smart-home-metrics charts/smart-home-metrics
```

## Architecture

### Application Structure

```
shm/
├── main.py              # FastAPI application and lifespan management
├── metrics.py           # SmartHomeCollector - main Prometheus collector
├── logging.py           # Custom log formatting with colorlog
└── collectors/          # Device-specific metric collectors
    ├── __init__.py      # MetricCollector abstract base class
    ├── smartthings.py   # SmartThings collector
    ├── ecobee.py        # Ecobee collector
    └── weatherapi.py    # WeatherAPI collector
```

### Collector Pattern

All collectors inherit from `MetricCollector` (shm/collectors/__init__.py) which provides:
- **Shared label infrastructure**: `label_names` and `default_documentation`
- **Metric helpers**: `get_gauge()` and `get_enum()` for creating metric families
- **Error handling**: `perform_collection()` wraps `collect_metrics()` and catches exceptions
- **Initialization hook**: `initialize()` for async setup

Each collector must:
1. Define `label_names` (list of label names for all metrics)
2. Define `default_documentation` (default metric description)
3. Implement `collect_metrics()` (async method that populates metrics)

### Metric Collection Flow

1. **Startup** (shm/main.py:13-20):
   - FastAPI lifespan context creates `SmartHomeCollector`
   - Calls `setup_collectors()` to initialize all enabled collectors in parallel
   - Registers collector with Prometheus REGISTRY

2. **Scrape Request**:
   - Prometheus scrapes the `/metrics` endpoint
   - `SmartHomeCollector.collect()` is called (synchronous, from prometheus_client)
   - Converts to async via `anyio.from_thread.run()`
   - Runs all collectors in parallel using `anyio.create_task_group()`
   - Aggregates and returns all metrics

3. **Collector Execution**:
   - Each collector's `perform_collection()` clears previous metrics
   - Calls `collect_metrics()` to fetch fresh data from APIs
   - Returns generated metrics (or empty list on error)

### Configuration

All collectors use `pydantic-settings` for environment-based configuration:

- **SmartThings**: `SMARTTHINGS_TOKEN`
- **Ecobee**:
  - `ECOBEE_CLIENT_ID`
  - `ECOBEE_TOKEN_STORE_TYPE` (file|kubernetes)
  - `ECOBEE_TOKEN_STORE_FILE_PATH` (default: ecobee.json)
  - `ECOBEE_TOKEN_STORE_K8S_NAMESPACE`
  - `ECOBEE_TOKEN_STORE_K8S_SECRET_NAME`

### Ecobee Token Management

The Ecobee collector (shm/collectors/ecobee.py) implements OAuth token management:
- **Storage backends**: File-based or Kubernetes Secret
- **Token refresh**: Automatic refresh with 30-second buffer before expiration
- **Caching**: Uses revision tracking to minimize API calls
- **Authorization flow**: Displays PIN code for initial authorization

### Key Design Patterns

1. **Async-first**: All I/O operations use async/await with `anyio` for parallelism
2. **Metric caching**: Collectors use `self.metrics` dict to cache metric families within a collection cycle
3. **Revision tracking**: Ecobee collector uses revision IDs to only fetch changed data
4. **Label consistency**: Each collector defines a fixed set of labels for all its metrics
5. **Error isolation**: Individual collector failures don't crash the entire scrape

## Deployment

### CI/CD Pipeline

**Build** (.github/workflows/build.yml):
- Runs on all branches and PRs
- Executes `make check` (isort, black, pylint, mypy)
- SonarCloud code quality scan
- Docker metadata generation for versioning

**Deploy** (.github/workflows/deploy.yml):
- Triggers on `main` branch push and tags
- Builds multi-arch Docker images (linux/amd64, linux/arm64)
- Pushes to `ghcr.io/clarkperkins/shm`
- Packages and pushes Helm chart to `ghcr.io/clarkperkins`

### Kubernetes Deployment

The service is deployed via Helm chart (charts/smart-home-metrics/):
- **ServiceMonitor**: Prometheus scrapes metrics every 60s with 30s timeout
- **Probes**: Liveness/readiness probes on `/health` with 60s initial delay
- **ServiceAccount**: Required for Kubernetes API access (Ecobee token storage)
- **Port**: Listens on 9000, exposed as ClusterIP service on port 80

### Python Environment

- **Version**: Python 3.12+
- **Package manager**: Poetry with PEP 621 pyproject.toml
- **Dependencies**: Listed in pyproject.toml dependencies array
- **Dev tools**: black, isort, mypy, pylint, pytest

### Tool Configuration

- **isort**: Uses black profile
- **mypy**: Checks untyped defs, ignores missing imports for kubernetes_asyncio, pyecobee, pysmartthings, uvicorn
- **coverage**: Branch coverage enabled, HTML output to reports/coverage/html

## Adding a New Collector

1. Create new file in `shm/collectors/` (e.g., `newdevice.py`)
2. Subclass `MetricCollector`:
   ```python
   class NewDeviceMetricCollector(MetricCollector):
       label_names = ["device_id", "device_name"]
       default_documentation = "NewDevice Metrics"
   ```
3. Implement configuration with `pydantic_settings.BaseSettings`
4. Implement `async def collect_metrics(self)` using `self.get_gauge()` / `self.get_enum()`
5. Add to `shm/metrics.py:27-36` in `setup_collectors()`
6. Add environment variables to Helm chart values/secrets
