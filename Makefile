PYTHON ?= python3

.PHONY: help install build build-nuitka build-docker smoke clean docker-build docker-test docker-shell

help:
	@printf '%s\n' \
		'Targets:' \
		'  make install        Install svb and dev tools into the current Python environment' \
		'  make build          Build ./dist/svb with PyInstaller' \
		'  make build-nuitka   Build ./dist/svb with Nuitka (alternative)' \
		'  make build-docker   Build ./dist/svb inside the dev container' \
		'  make smoke          Build, then run ./dist/svb --help' \
		'  make clean          Remove build artifacts' \
		'  make docker-build   Build the development container image' \
		'  make docker-test    Run svb --help inside the dev container' \
		'  make docker-shell   Open a shell inside the dev container'

install:
	$(PYTHON) -m pip install -e '.[dev]'

build:
	pyinstaller --onefile --name svb --distpath dist --workpath build/pyinstaller --specpath build src/svb/__main__.py

build-nuitka:
	nuitka --onefile --output-dir=dist --output-filename=svb src/svb/__main__.py

build-docker:
	docker compose run --rm svb make build

smoke: build
	./dist/svb --help

clean:
	rm -rf build dist

docker-build:
	docker compose build

docker-test: docker-build
	docker compose run --rm svb svb --help

docker-shell: docker-build
	docker compose run --rm svb bash
