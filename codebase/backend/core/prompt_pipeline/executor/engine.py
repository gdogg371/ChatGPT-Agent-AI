from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List
from collections import defaultdict

from v2.backend.core.configuration.config import PatchLoopConfig
from v2.backend.core.utils.logging.logging import ConsoleLog
from v2.backend.core.prompt_pipeline.executor.errors import LlmClientError, IoError, ValidationError
from v2.backend.core.prompt_pipeline.preflight.budget import TokenBudgeter

from v2.backend.core.utils.io.run_dir import RunDirs
from v2.backend.core.utils.io.file_ops import FileOps, FileOpsConfig
from v2.backend.core.utils.io.output_writer import OutputWriter
from v2.backend.core.utils.io.patch_ops import PatchOps

from .sources import IntrospectionDbSource
from .retriever import SourceRetriever
from .steps import BuildContextStep, PackPromptStep, UnpackResultsStep
from .rewrite import apply_docstring_update        # remains via alias or direct module
from v2.backend.core.docstrings.verify import DocstringVerifier  # moved to docstrings
from v2.backend.core.docstrings.sanitize import sanitize_docstring  # moved to docstrings

from v2.backend.core.prompt_pipeline.llm.router import ModelRouter, RoutingContext
from v2.backend.core.prompt_pipeline.llm.client import LlmClient, LlmRequest
from v2.backend.core.prompt_pipeline.llm.schema import openai_response_format

from importlib import import_module
from .plugin_api import TaskAdapter

def _load_plugin(path: str) -> TaskAdapter:
    mod, _, cls = path.partition(":")
    if not mod or not cls:
        raise ValueError(f"Invalid plugin spec: {path!r} (expected 'package.module:Class')")
    module = import_module(mod)
    klass = getattr(module, cls)
    return klass()


@dataclass
class Engine:
    cfg: PatchLoopConfig

    def _split_batches_to_fit_output(self, batches: List[List[Dict]]) -> List[List[Dict]]:
        """
        Ensure each batch's estimated output fits within cfg.max_output_tokens.
        Conservative estimate: response_tokens_per_item * items + batch_overhead_tokens.
        If any batch exceeds, split into single-item batches.
        """
        max_out = int(getattr(self.cfg, "max_output_tokens", 1024))
        per_item = int(getattr(self.cfg, "response_tokens_per_item", 320))
        overhead = int(getattr(self.cfg, "batch_overhead_tokens", 64))

        safe: List[List[Dict]] = []
        for batch in batches:
            est = per_item * len(batch) + overhead
            if est <= max_out:
                safe.append(batch)
            else:
                for it in batch:
                    safe.append([it])
        return safe

    def run(self) -> Path:
        self.cfg.normalize()
        log = ConsoleLog("Orchestrator")

        # Flag snapshot (single f-string: ConsoleLog.info is single-arg)
        log.info(
            f"Flags: scan={self.cfg.run_scan_docstrings} "
            f"get_code={self.cfg.run_get_code_for_docstrings} "
            f"build={self.cfg.run_build_prompts} "
            f"run_llm={self.cfg.run_run_llm} "
            f"save_patch={self.cfg.run_save_patch} "
            f"sandbox={self.cfg.run_apply_patch_sandbox} "
            f"verify={self.cfg.run_verify_docstring} "
            f"archive={self.cfg.run_archive_and_replace} "
            f"rollback={self.cfg.run_rollback} "
            f"verbose={self.cfg.verbose} "
            f"max_rows={self.cfg.max_rows}"
        )

        # --- Prepare run directories
        run_dirs = RunDirs(self.cfg.out_base)
        run_id = run_dirs.make_run_id(self.cfg.run_id_suffix)
        dirs = run_dirs.ensure(run_id)
        log.stage("📂", f"Run directory: {dirs.root}")

        file_ops = FileOps(FileOpsConfig(preserve_crlf=self.cfg.preserve_crlf))
        out = OutputWriter(dirs.root)
        patch_ops = PatchOps(file_ops)

        # --- Step 1: external scanner (no-op placeholder)
        if self.cfg.run_scan_docstrings:
            log.stage("🔎", "Step 1 Scan: external scanner not integrated here — skipping (no-op).")

        # --- Step 2: read from DB and enrich with code
        if not self.cfg.run_get_code_for_docstrings:
            log.stage("⏭️", "Step 2 Get Code: skipped by flag.")
            suspects: List[Dict] = []
        else:
            log.stage("🛢️", f"Step 2 Get Code: reading rows from {self.cfg.sqlalchemy_url} / {self.cfg.sqlalchemy_table}")
            src = IntrospectionDbSource(
                url=self.cfg.sqlalchemy_url,
                table=self.cfg.sqlalchemy_table,
                status_filter=self.cfg.status_filter,
                max_rows=self.cfg.max_rows,
            )
            retriever = SourceRetriever(project_root=self.cfg.project_root, file_ops=file_ops)

            suspects = []
            for row in src.read_rows():
                try:
                    suspects.append(retriever.enrich(row))
                except IoError as e:
                    log.warn(f"Skipping row id={row.get('id')}: {e}")
            log.info(f"Loaded {len(suspects)} suspect(s) from DB")

        # Early exit
        if not suspects:
            log.warn("No suspects to process. Exiting early.")
            return dirs.root

        # --- Step 3: build prompts (context + packing)
        if not self.cfg.run_build_prompts:
            log.stage("⏭️", "Step 3 Build Prompts: skipped by flag.")
            batches: List[List[Dict]] = []
            packed_prompts: List[Dict] = []
        else:
            log.stage("🧱", "Step 3 Build Prompts: building items and batching")
            items = BuildContextStep().run(suspects)

            # Persist each item for traceability
            for it in items:
                out.write_item(it)

            packer = PackPromptStep()
            budget = TokenBudgeter(
                max_ctx=self.cfg.model_ctx_tokens,
                resp_per_item=self.cfg.response_tokens_per_item,
                guardrail=self.cfg.budget_guardrail,
                batch_overhead_tokens=self.cfg.batch_overhead_tokens,
            )

            def _ser(lst: List[Dict]) -> str:
                return packer.serialize_items(lst)

            batches = budget.pack(items, _ser)
            batches = self._split_batches_to_fit_output(batches)  # ensure outputs fit
            packed_prompts = [packer.build(batch) for batch in batches]

            log.info(f"Prepared {len(packed_prompts)} batch(es)")
            for i, pb in enumerate(packed_prompts, 1):
                prompt_path = dirs.root / "raw_prompts" / f"batch_{i:03d}.txt"
                text = pb["messages"]["system"] + "\n\n" + pb["messages"]["user"]
                file_ops.write_text(prompt_path, text)
                if self.cfg.verbose:
                    log.stage("📝", f"Prompt {i} saved: {prompt_path}")

        # --- Step 4: call LLM
        router = ModelRouter(default=self.cfg.model)
        client = LlmClient(provider=self.cfg.provider)

        all_results: Dict[str, Dict] = {}
        for i, bundle in enumerate(packed_prompts, 1):
            if not self.cfg.run_run_llm:
                log.stage("⏭️", f"Step 4 Run LLM: skipped by flag for batch {i}.")
                continue

            ctx = RoutingContext(
                task_type="docstring_rewrite",
                avg_input_tokens=0,
                needs_vision=False,
                needs_tools=False,
                cost_tier="low",
            )
            model = router.choose(ctx, override=self.cfg.model)

            # conservative per-batch output cap
            per_item = int(getattr(self.cfg, "response_tokens_per_item", 320))
            overhead = int(getattr(self.cfg, "batch_overhead_tokens", 64))
            est_out = per_item * len(bundle["batch"]) + overhead
            req_max = max(min(self.cfg.max_output_tokens, est_out + 128), 256)

            req = LlmRequest(
                system=bundle["messages"]["system"],
                user=bundle["messages"]["user"],
                model=model,
                max_output_tokens=req_max,
                api_key=self.cfg.api_key,
                response_format=openai_response_format(expected_ids=bundle["ids"]),
            )

            log.stage("🚀", f"Batch {i}: sending to model '{model}' via provider '{self.cfg.provider}'")
            try:
                raw = client.complete(req)
            except (LlmClientError, Exception) as e:
                log.error(f"LLM call failed for batch {i}: {e}")
                continue

            # persist raw response
            resp_path = dirs.root / "raw_responses" / f"batch_{i:03d}.json"
            file_ops.write_text(resp_path, raw)

            # parse & validate (with salvage inside)
            try:
                expected_ids = bundle["ids"]
                parsed = UnpackResultsStep(expected_ids=expected_ids).run(raw)
            except ValidationError as e:
                if self.cfg.verbose:
                    log.error(f"Batch {i} response validation failed: {e}")
                    hint = dirs.root / "raw_responses" / f"batch_{i:03d}.hint.txt"
                    file_ops.write_text(hint, raw[:1000])
                continue

            for item in parsed:
                all_results[item["id"]] = item
            log.info(f"Batch {i}: parsed {len(parsed)} item(s)")

        if not all_results and self.cfg.run_run_llm:
            log.warn("No parsed results from LLM. Exiting after Step 4.")
            return dirs.root

        # Map items by id for quick lookup
        item_by_id: Dict[str, Dict] = {}
        for batch in batches:
            for it in batch:
                item_by_id[it["id"]] = it

        verifier = DocstringVerifier()

        # --- Steps 5–7: accumulate edits per file, then write patches/apply once per file
        by_file: Dict[str, List[Dict]] = defaultdict(list)
        for rid, result in all_results.items():
            it = item_by_id.get(rid)
            if not it:
                log.warn(f"Result id not found in items: {rid}")
                continue
            by_file[it["relpath"]].append({
                "id": rid,
                "relpath": it["relpath"],
                "path": Path(it["path"]),
                "signature": it["signature"],
                "target_lineno": int(it["target_lineno"]),
                "mode": result.get("mode", "rewrite"),
                "raw_doc": result["docstring"],
            })

        for relpath, entries in by_file.items():
            # read file once
            path = entries[0]["path"]
            try:
                original_src = file_ops.read_text(path)
            except Exception as e:
                log.error(f"Failed to read source for {relpath}: {e}")
                continue

            # bottom-up application to keep line numbers stable
            entries_sorted = sorted(entries, key=lambda x: x["target_lineno"], reverse=True)
            aggregated_src = original_src

            # per-item patches (against ORIGINAL), and aggregate into aggregated_src
            for e in entries_sorted:
                rid = e["id"]
                sig = e["signature"]
                doc = sanitize_docstring(e["raw_doc"], signature=sig)

                # single-item updated view off the ORIGINAL source
                item_src = apply_docstring_update(original_src, e["target_lineno"], doc, relpath=relpath)

                # per-item patch (optional)
                if self.cfg.run_save_patch and getattr(self.cfg, "save_per_item_patches", True):
                    rel_sanitized = relpath.replace("\\", "__").replace("/", "__")
                    item_patch = patch_ops.write_patch(
                        dirs.root, rel_sanitized, original_src, item_src, f"{relpath}#{rid}", per_item_suffix=f"__{rid}"
                    )
                    out.append_summary(rid, relpath, sig, str(item_patch), True, "")
                    log.stage("🧩", f"Per-item patch written: {item_patch}")

                # apply into aggregated result
                aggregated_src = apply_docstring_update(aggregated_src, e["target_lineno"], doc, relpath=relpath)

            # combined patch (optional)
            if self.cfg.run_save_patch and getattr(self.cfg, "save_combined_patch", True):
                rel_sanitized = relpath.replace("\\", "__").replace("/", "__")
                combined_patch = patch_ops.write_patch(dirs.root, rel_sanitized, original_src, aggregated_src, relpath)
                log.stage("🧩", f"Combined patch written: {combined_patch}")

            # apply once to sandbox
            if self.cfg.run_apply_patch_sandbox:
                applied = patch_ops.apply_to_sandbox(dirs.root, relpath, aggregated_src)
                log.stage("🧪", f"Sandbox applied: {applied}")

            # verify each item's docstring text
            if self.cfg.run_verify_docstring:
                for e in entries_sorted:
                    rid = e["id"]
                    sig = e["signature"]
                    doc = sanitize_docstring(e["raw_doc"], signature=sig)
                    ok1, issues1 = verifier.pep257_minimal(doc)
                    ok2, issues2 = verifier.params_consistency(doc, sig)
                    issues = issues1 + issues2
                    rep = dirs.root / "verify_reports" / f"{rid}.txt"
                    lines = [
                        f"FILE: {relpath}",
                        f"ID: {rid}",
                        f"SIGNATURE: {sig}",
                        "RESULT: " + ("OK" if (ok1 and ok2) else "FAIL"),
                    ]
                    if issues:
                        lines.append("ISSUES:")
                        lines.extend([f" - {x}" for x in issues])
                    else:
                        lines.append("ISSUES: (none)")
                    (dirs.root / "verify reports").mkdir(exist_ok=True)
                    file_ops.write_text(rep, "\n".join(lines))
                    log.stage("🔍", f"Verify report: {rep}")

            # Step 8: archive & replace (guarded)
            if self.cfg.run_archive_and_replace:
                if not self.cfg.confirm_prod_writes:
                    log.warn("Step 8 Archive/Replace requested but confirm_prod_writes=False; skipping.")
                else:
                    # Archive original
                    arc_path = dirs.root / "archives" / relpath
                    arc_path.parent.mkdir(parents=True, exist_ok=True)
                    file_ops.write_text(arc_path, original_src, preserve_crlf=self.cfg.preserve_crlf)
                    # Replace in-place
                    try:
                        file_ops.write_text(path, aggregated_src, preserve_crlf=self.cfg.preserve_crlf)
                        # Mirror to prod_applied
                        prod_copy = dirs.root / "prod_applied" / relpath
                        prod_copy.parent.mkdir(parents=True, exist_ok=True)
                        file_ops.write_text(prod_copy, aggregated_src, preserve_crlf=self.cfg.preserve_crlf)
                        log.stage("📦", f"Archived → {arc_path}; Replaced source; Mirror → {prod_copy}")
                    except Exception as e:
                        log.error(f"Archive/Replace failed for {relpath}: {e}")

        # --- Step 9: rollback placeholder
        if self.cfg.run_rollback:
            if not self.cfg.confirm_prod_writes:
                log.warn("Step 9 Rollback requested but confirm_prod_writes=False; skipping.")
            else:
                log.stage("↩️", "Rollback step is placeholder (use archived originals in this run directory).")

        log.info("Pipeline completed")
        return dirs.root
