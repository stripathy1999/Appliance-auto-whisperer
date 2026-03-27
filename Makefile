.PHONY: install dev run test lint docker-build docker-build-bureau smoke

install:
	python -m pip install -r requirements.txt
	python -m pip install -e ".[dev]"

dev: install
	python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000

run:
	python -m uvicorn main:app --host 0.0.0.0 --port 8000

test:
	python -m pytest tests -v

lint:
	python -m ruff check app tests

docker-build:
	docker build -t appliance-auto-whisperer:local .

docker-build-bureau:
	docker build -f Dockerfile.bureau -t repair-orchestrator-bureau:local .

smoke:
	python scripts/smoke_test.py
