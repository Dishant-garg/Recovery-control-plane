PY    := .venv/bin/python
SEED  ?= 42
SEEDS ?=

.PHONY: setup test verify-audit clean help data baseline eval demo scale analyze llm-check
.PHONY: live sensitivity

help:
	@echo "  make setup         venv + dependencies"
	@echo "  make data          generate seeded synthetic events (SEED=$(SEED))"
	@echo "  make eval          baseline vs control plane, 20 seeds"
	@echo "  make baseline      baseline only"
	@echo "  make demo          data + eval"
	@echo "  make test          full invariant suite"
	@echo "  make verify-audit  recompute the audit hash chain (exit 1 on tamper)"
	@echo "  make sensitivity   break-even on the churn assumption"
	@echo "  make analyze       agent investigates where the control plane loses"
	@echo "  make llm-check     one live tool-use round trip (RCP_LLM=groq etc)"
	@echo "  make demo-agent    recovery agent decides 5 cases live"
	@echo "  make dashboard     live dashboard on http://localhost:8000"
	@echo "  make drafts        composer agent drafts missing templates"
	@echo "  make strategy      audit the proposers' hand-written tables"
	@echo "  make redteam       attack the compliance critic to find its holes"
	@echo "  make voice         render the spoken templates to audio (macOS)"
	@echo "  make live          real Razorpay test-mode Payment Links"
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

sensitivity:
	$(PY) -m eval.sensitivity

analyze:
	$(PY) -m eval.analyst

llm-check:
	$(PY) -m rcp.llm.check

live:
	@echo "Sending real Razorpay test-mode Payment Links. Ctrl-C to stop."
	RCP_EXECUTOR=razorpay_rest $(PY) -m eval.run --mode control_plane --seed $(SEED)

demo-agent:
	$(PY) -m eval.run --mode control_plane --seed $(SEED) --live 5

dashboard:
	$(PY) -m uvicorn app.main:app --reload --port 8000

drafts:
	$(PY) -m rcp.agents.composer --max-drafts 6

strategy:
	$(PY) -m rcp.agents.strategy --seed $(SEED) --arm baseline

redteam:
	$(PY) -m rcp.agents.redteam --max-attacks 8

voice:
	$(PY) -m scripts.generate_voice
