.PHONY: dev test lint install install-uv uninstall

# Editable install into a local venv, for hacking on the orchestrator itself.
dev:
	python3 -m venv .venv
	.venv/bin/pip install -q -e ".[dev]"

test: dev
	.venv/bin/python test_stargate.py

lint: dev
	.venv/bin/ruff check .

install:
	pipx install --force .

install-uv:
	uv tool install --force .

uninstall:
	-pipx uninstall stargate
	-uv tool uninstall stargate
