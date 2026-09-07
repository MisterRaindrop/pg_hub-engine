.PHONY: demo test doctor

demo:
	python3 -m pg_hub demo --reset --twice

test:
	python3 -m unittest discover -v

doctor:
	python3 -m pg_hub doctor
