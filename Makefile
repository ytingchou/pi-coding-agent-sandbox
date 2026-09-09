UV ?= uv
HADOLINT_IMAGE ?= hadolint/hadolint:v2.12.0-debian

.PHONY: sync lock format lint lint-python lint-yaml lint-docker test test-container check
sync:
	$(UV) sync --locked
lock:
	$(UV) lock
format:
	$(UV) run --locked ruff check --fix .
	$(UV) run --locked ruff format .
lint: lint-python lint-yaml lint-docker lint-helm
lint-python:
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .
lint-yaml:
	$(UV) run --locked yamllint --strict .
lint-docker:
	docker run --rm --network none -v "$(CURDIR):/work:ro" -w /work $(HADOLINT_IMAGE) hadolint --config .hadolint.yaml orchestrator/Dockerfile sandbox/Dockerfile
test:
	$(UV) run --locked pytest
test-container:
	docker compose -f compose.yaml -f compose.offline-test.yaml build sandbox-1
	docker compose -f compose.yaml -f compose.offline-test.yaml run --rm --no-deps -e RUN_ISOLATION_TESTS=1 sandbox-1 /opt/server/bin/python -m pytest -p no:cacheprovider -q
check: lint test

.PHONY: lint-helm
lint-helm:
	$(UV) run --locked python scripts/check_helm.py
