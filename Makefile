PY := .venv/bin/python
SEED ?= 42

.PHONY: setup test verify-audit clean help
.PHONY: data baseline eval demo live sensitivity scale

help:
	@echo "Working today:"
	@echo "  make setup         venv + dependencies"
	@echo "  make test          full invariant suite"
	@echo "  make verify-audit  recompute the audit hash chain (exit 1 on tamper)"
	@echo "  make clean         drop generated data for SEED=$(SEED)"
	@echo ""
	@echo "Pending (need sim/ and eval/):"
	@echo "  make data baseline eval demo live sensitivity scale"

setup:
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt

test:
	$(PY) -m pytest -q

verify-audit:
	$(PY) -m rcp.audit $(SEED)

clean:
	rm -rf data/seed_$(SEED)

# ---------------------------------------------------------------------------
# Not yet implemented. Each fails loudly rather than silently doing nothing,
# so a reviewer following the README never sees a no-op success.
# ---------------------------------------------------------------------------
data baseline eval demo live sensitivity scale:
	@echo "'$@' is not implemented yet -- sim/ and eval/ are the next milestone."
	@exit 1
