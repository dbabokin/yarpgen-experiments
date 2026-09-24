.PHONY: smoke generate campaign

smoke:
	bash scripts/smoke.sh

generate:
	python3 -m yarpgen --seed 1 --lang c --profile smoke --out /tmp/yarpgen-out

campaign:
	bash scripts/campaign-local.sh
