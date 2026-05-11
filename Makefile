# Makefile for memsearch development
#
# Usage:
#   make install    Symlink local plugin into OpenCode's npm cache
#   make status     Show current plugin linkage and daemon state
#   make flush      Flush pending summary jobs across all projects

REPO_ROOT    := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PLUGIN_DIR   := $(REPO_ROOT)/plugins/opencode
CACHE_BASE   := $(HOME)/.cache/opencode/packages/@zilliz/memsearch-opencode@latest/node_modules/@zilliz

# All project dirs that have .memsearch capture state
PROJECT_DIRS := \
	$(HOME)/projects/trading \
	$(HOME)/projects/trading/guineapigging \
	$(HOME)/projects/trading/execution

.PHONY: install status flush

##############################################################################
# install — symlink the repo's plugin dir into OpenCode's npm cache
##############################################################################

install:
	@echo "=== Installing memsearch OpenCode plugin from local repo ==="
	@echo ""
	@# Step 1: Back up existing cached directory (if it's a real dir, not already a symlink)
	@if [ -d "$(CACHE_BASE)/memsearch-opencode" ] && [ ! -L "$(CACHE_BASE)/memsearch-opencode" ]; then \
		if [ -d "$(CACHE_BASE)/memsearch-opencode.backup" ]; then \
			echo "[INFO] Removing previous backup"; \
			rm -rf "$(CACHE_BASE)/memsearch-opencode.backup"; \
		fi; \
		mv "$(CACHE_BASE)/memsearch-opencode" "$(CACHE_BASE)/memsearch-opencode.backup"; \
		echo "[OK] Backed up npm cache to memsearch-opencode.backup"; \
	elif [ -L "$(CACHE_BASE)/memsearch-opencode" ]; then \
		echo "[INFO] Existing symlink found, removing..."; \
		rm "$(CACHE_BASE)/memsearch-opencode"; \
	fi
	@# Step 2: Create symlink to repo
	@ln -s "$(PLUGIN_DIR)" "$(CACHE_BASE)/memsearch-opencode"
	@echo "[OK] Symlinked: $(CACHE_BASE)/memsearch-opencode -> $(PLUGIN_DIR)"
	@# Step 3: Verify key files are accessible through the symlink
	@if [ -f "$(CACHE_BASE)/memsearch-opencode/index.ts" ]; then \
		echo "[OK] index.ts accessible"; \
	else \
		echo "[ERROR] index.ts NOT accessible through symlink"; \
		exit 1; \
	fi
	@if [ -f "$(CACHE_BASE)/memsearch-opencode/scripts/capture-daemon.py" ]; then \
		echo "[OK] capture-daemon.py accessible"; \
	else \
		echo "[ERROR] capture-daemon.py NOT accessible through symlink"; \
		exit 1; \
	fi
	@# Step 4: Confirm the daemon code is the NEW version (has submit_turn_to_service)
	@if grep -q "submit_turn_to_service" "$(CACHE_BASE)/memsearch-opencode/scripts/capture-daemon.py"; then \
		echo "[OK] capture-daemon.py is the new version (uses submit-turn flow)"; \
	else \
		echo "[WARN] capture-daemon.py does not contain submit_turn_to_service — may be old version"; \
	fi
	@# Step 5: Symlink @opencode-ai/plugin dependency so Node can resolve imports from repo dir
	@mkdir -p "$(PLUGIN_DIR)/node_modules/@opencode-ai"
	@if [ -d "$(HOME)/.cache/opencode/packages/@zilliz/memsearch-opencode@latest/node_modules/@opencode-ai/plugin" ]; then \
		if [ -L "$(PLUGIN_DIR)/node_modules/@opencode-ai/plugin" ] || [ -e "$(PLUGIN_DIR)/node_modules/@opencode-ai/plugin" ]; then \
			rm -rf "$(PLUGIN_DIR)/node_modules/@opencode-ai/plugin"; \
		fi; \
		ln -s "$(HOME)/.cache/opencode/packages/@zilliz/memsearch-opencode@latest/node_modules/@opencode-ai/plugin" "$(PLUGIN_DIR)/node_modules/@opencode-ai/plugin"; \
		echo "[OK] Symlinked @opencode-ai/plugin dependency"; \
	else \
		echo "[ERROR] @opencode-ai/plugin not found in npm cache — plugin will fail to load"; \
		exit 1; \
	fi
	@echo ""
	@echo "Done. Restart OpenCode or kill running daemons to pick up the new code."
	@echo ""

##############################################################################
# status — show current linkage and daemon state
##############################################################################

status:
	@echo "=== memsearch plugin status ==="
	@echo ""
	@echo "Plugin path:"
	@ls -la "$(CACHE_BASE)/memsearch-opencode" 2>/dev/null || echo "  Not found"
	@echo ""
	@echo "capture-daemon.py version:"
	@if [ -f "$(CACHE_BASE)/memsearch-opencode/scripts/capture-daemon.py" ]; then \
		lines=$$(wc -l < "$(CACHE_BASE)/memsearch-opencode/scripts/capture-daemon.py"); \
		if grep -q "submit_turn_to_service" "$(CACHE_BASE)/memsearch-opencode/scripts/capture-daemon.py"; then \
			echo "  $$lines lines — NEW version (submit-turn flow)"; \
		else \
			echo "  $$lines lines — OLD version (local summarize)"; \
		fi; \
	else \
		echo "  Not found"; \
	fi
	@echo ""
	@echo "Running capture daemons:"
	@ps aux | grep capture-daemon.py | grep -v grep | sed 's/^/  /' || echo "  None running"
	@echo ""
	@echo "Pending summary jobs:"
	@pending_found=0; \
	for dir in $(HOME)/.memsearch/spool/summaries/pending \
		$(HOME)/projects/trading/.memsearch/spool/summaries/pending \
		$(HOME)/projects/trading/guineapigging/.memsearch/spool/summaries/pending \
		$(HOME)/projects/trading/execution/.memsearch/spool/summaries/pending; do \
		if [ -d "$$dir" ]; then \
			count=$$(find "$$dir" -name '*.json' 2>/dev/null | wc -l | tr -d ' '); \
			if [ "$$count" -gt 0 ]; then \
				echo "  $$dir: $$count pending"; \
				pending_found=1; \
			fi; \
		fi; \
	done; \
	if [ "$$pending_found" -eq 0 ]; then \
		echo "  None"; \
	fi
	@echo ""

##############################################################################
# flush — retry all pending summary jobs
##############################################################################

flush:
	@echo "=== Flushing pending summary jobs ==="
	@echo ""
	@for dir in $(HOME)/.memsearch/spool/summaries/pending \
		$(HOME)/projects/trading/.memsearch/spool/summaries/pending \
		$(HOME)/projects/trading/guineapigging/.memsearch/spool/summaries/pending \
		$(HOME)/projects/trading/execution/.memsearch/spool/summaries/pending; do \
		if [ -d "$$dir" ]; then \
			count=$$(find "$$dir" -name '*.json' 2>/dev/null | wc -l | tr -d ' '); \
			if [ "$$count" -gt 0 ]; then \
				echo "Found $$count pending job(s) in $$dir"; \
				memory_dir=$$(dirname $$(dirname $$(dirname "$$dir")))/memory; \
				for f in "$$memory_dir"/2026-*.md; do \
					if [ -f "$$f" ]; then \
						echo "  Flushing: $$f"; \
						memsearch flush-turns "$$f" 2>&1 || true; \
					fi; \
				done; \
			else \
				echo "No pending jobs in $$dir"; \
			fi; \
		fi; \
	done
	@echo ""
	@echo "Done."
