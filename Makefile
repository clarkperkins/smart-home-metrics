
clean:
	rm -rf .coverage .mypy_cache .pytest_cache reports build dist

reports:
	mkdir -p reports
	mkdir -p reports/coverage
	mkdir -p reports/tests

format/isort:
	uv run isort shm tests

format/black:
	uv run black shm tests

format: format/isort format/black

check/isort:
	uv run isort shm tests --check

check/black:
	uv run black shm tests --check

check/pylint:
	uv run pylint shm --reports=n --fail-under=0 --fail-on=E

check/mypy:
	uv run mypy shm tests

check: check/isort check/black check/pylint check/mypy

test/pytest/xml: reports
	uv run pytest --junit-xml=reports/tests/unit.xml --cov=shm --cov-report=xml

test/pytest/html: reports
	uv run pytest --cov=shm --cov-report=html

test: test/pytest/xml

cov: test/pytest/html
	open reports/coverage/html/index.html
