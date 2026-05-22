.PHONY: init verify run report doctor clean help

PY := .venv/bin/python
PYTEST := .venv/bin/pytest
PIPELINE_OUTPUT ?= /Users/alexandergo/Documents/Doctorat/pipeline/eval/work

help:
	@echo "eval-agent — long-running Gemini evaluation agent"
	@echo
	@echo "  make init           bootstrap (idempotent: venv, deps, state files, git init)"
	@echo "  make verify         session-startup pre-flight (cache, schemas, fixtures, tests)"
	@echo "  make run            evaluate the default pipeline run; PIPELINE_OUTPUT=<dir> to override"
	@echo "  make report         regenerate human reports from the latest run"
	@echo "  make doctor         health check: API key, cache reachable, schemas valid"
	@echo "  make clean          remove .venv, caches, build artefacts (keeps state/)"

init:
	bash init.sh

verify:
	$(PY) -m eval_agent.cli verify

run:
	$(PY) -m eval_agent.cli run --pipeline-output "$(PIPELINE_OUTPUT)"

report:
	$(PY) -m eval_agent.cli report --run latest

doctor:
	$(PY) -m eval_agent.cli doctor

test:
	PYTHONPATH=. $(PYTEST)

lint:
	.venv/bin/ruff check eval_agent tests
	.venv/bin/ruff format --check eval_agent tests

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache .coverage build dist *.egg-info
	find . -name __pycache__ -type d -exec rm -rf {} +
