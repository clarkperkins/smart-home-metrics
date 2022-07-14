
clean:
	rm -rf .coverage .mypy_cache .pytest_cache reports build dist

reports:
	mkdir -p reports
	mkdir -p reports/coverage
	mkdir -p reports/tests

format/isort:
	poetry run isort shm

format/black:
	poetry run black shm

format: format/isort format/black

check/isort:
	poetry run isort shm --check

check/black:
	poetry run black shm --check

check/pylint: reports
	poetry run pylint shm --reports=n --exit-zero --msg-template="{path}:{line}: [{msg_id}({symbol}), {obj}] {msg}" > reports/pylint.txt

check/mypy:
	poetry run mypy shm

check: check/isort check/black check/pylint check/mypy

test/pytest/xml: reports
	poetry run pytest --junit-xml=reports/tests/unit.xml --cov=shm --cov-report=xml

test/pytest/html: reports
	poetry run pytest --cov=shm --cov-report=html

test: test/pytest/xml

cov: test/pytest/html
	open reports/coverage/html/index.html
