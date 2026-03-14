.PHONY: run revision upgrade downgrade

UVICORN = .venv/bin/uvicorn
ALEMBIC = .venv/bin/alembic

run:
	$(UVICORN) main:app --host 0.0.0.0 --port 8000 --reload

revision:
	@if [ -z "$(msg)" ]; then echo 'Usage: make revision msg="message"'; exit 1; fi
	$(ALEMBIC) revision --autogenerate -m "$(msg)"

upgrade:
	$(ALEMBIC) upgrade head

downgrade:
	$(ALEMBIC) downgrade -1
