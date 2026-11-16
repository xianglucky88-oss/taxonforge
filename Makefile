PYTHON ?= python3
.PHONY: test demo check
test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v
demo:
	PYTHONPATH=src $(PYTHON) examples/smoke.py
check: test demo
