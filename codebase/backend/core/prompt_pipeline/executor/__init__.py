import sys as _sys
_aliases = {
    "patch_loop_test2.executor.ast_utils":   "patch_loop_test2.docstrings.ast_utils",
    "patch_loop_test2.executor.sanitize":    "patch_loop_test2.docstrings.sanitize",
    "patch_loop_test2.executor.verify":      "patch_loop_test2.docstrings.verify",
    "patch_loop_test2.executor.prompts":     "patch_loop_test2.docstrings.prompt_builder",
}
for k, v in list(_aliases.items()):
    if k not in _sys.modules:
        try:
            __import__(v)
            _sys.modules[k] = _sys.modules[v]
        except Exception:
            pass
del _sys, _aliases
