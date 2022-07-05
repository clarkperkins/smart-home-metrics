
clean:
	rm -rf .coverage .mypy_cache .pytest_cache reports build dist

reports:
	mkdir -p reports
	mkdir -p reports/coverage
	mkdir -p reports/tests

format/isort:
	poetry run isort stpm

format/black:
	poetry run black stpm

format: format/isort format/black

check/isort:
	poetry run isort stpm --check

check/black:
	poetry run black stpm --check

check/pylint: reports
	poetry run pylint stpm --reports=n --exit-zero --msg-template="{path}:{line}: [{msg_id}({symbol}), {obj}] {msg}" > reports/pylint.txt

check/mypy:
	poetry run mypy stpm

check: check/isort check/black check/pylint check/mypy

test/pytest/xml: reports
	poetry run pytest --junit-xml=reports/tests/unit.xml --cov=stpm --cov-report=xml

test/pytest/html: reports
	poetry run pytest --cov=stpm --cov-report=html

test: test/pytest/xml

cov: test/pytest/html
	open reports/coverage/html/index.html
