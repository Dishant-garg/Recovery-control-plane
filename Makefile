PY    := .venv/bin/python
SEED  ?= 42
SEEDS ?=

.PHONY: setup test verify-audit clean help data baseline eval demo scale
.PHONY: live sensitivity

help:
	@echo "  make setup         venv + dependencies"
	@echo "  make data          generate seeded synthetic events (SEED=$(SEED))"
	@echo "  make eval          baseline vs control plane, 20 seeds"
	@echo "  make baseline      baseline only"
	@echo "  make demo          data + eval"
	@echo "  make test          full invariant suite"
	@echo "  make verify-audit  recompute the audit hash chain (exit 1 on tamper)"
	@echo "  make scale         100k-event stress run for docs/SCALE.md"
	@echo "  make clean         drop generated data"

setup:
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt

data:
	$(PY) -m sim.generate --seed $(SEED)

eval:
	$(PY) -m eval.run $(if $(SEEDS),--seeds $(SEEDS),)

baseline:
	$(PY) -m eval.run --mode baseline --seed $(SEED)

demo: data eval

test:
	$(PY) -m pytest -q

verify-audit:
	$(PY) -m rcp.audit $(SEED)

scale:
	$(PY) -m sim.generate --seed $(SEED) --scale 100000

clean:
	rm -rf data/seed_* data/scale

# Not yet implemented -- fail loudly rather than silently doing nothing.
live sensitivity:
	@echo "'$@' needs rcp/execute/razorpay_*.py and eval/sensitivity.py."
	@exit 1
