# tools/who_is_importing.py
import importlib, sys
names = [
    "v2.backend.core.prompt_pipeline.executor.engine",
    "v2.backend.core.prompt_pipeline.executor.providers",
    "v2.backend.core.prompt_pipeline.llm.providers",
]
for n in names:
    m = importlib.import_module(n)
    print(f"{n} => {m.__file__}")
