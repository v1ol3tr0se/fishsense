RUFF ?= uvx ruff@0.17.3
PYTHON ?= python3
FISH_FILES := $(wildcard conf.d/*.fish functions/*.fish completions/*.fish tests/fixtures/params/src/*.fish)

.PHONY: verify format

verify:
	for f in $(FISH_FILES); do fish -n "$$f" || exit 1; done
	fish_indent --check $(FISH_FILES)
	$(RUFF) format --check .
	$(RUFF) check .
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests

format:
	fish_indent -w $(FISH_FILES)
	$(RUFF) format .
