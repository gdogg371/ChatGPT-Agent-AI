from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List
from collections import defaultdict
import copy

from v2.backend.core.configuration.config import PatchLoopConfig
from v2.backend.core.utils.logging.logging import ConsoleLog
from v2.backend.core.prompt_pipeline.executor.errors import (
    LlmClientError,
    IoError,
    ValidationError,
    AskSpecError,
)
from v2.backend.core.prompt_pipeline.preflight.budget import TokenBudgeter
from v2.backend.core.utils.io.run_dir import RunDirs
from v2.backend.core.utils.io.file_ops import FileOps, FileOpsConfig
from v2.backend.core.utils.io.output_writer import OutputWriter
from v2.patches.core.patch_ops import PatchOps

from .sources import IntrospectionDbSource
from .retriever import SourceRetriever
from .steps import BuildContextStep, PackPromptStep  # parsing delegated to adapter

# LLM routing
from v2.backend.core.prompt_pipeline.llm.router import select_route
from v2.backend.core.prompt_pipeline.llm.client import LlmClient, LlmRequest

# Task adapters
from v2.backend.core.tasks.registry import select_task_adapter


def _tighten_response_format_with_ids(rf: dict | None, expected_ids: List[str]) -> dict | None:
    """
    If a JSON-schema response_format is present, inject an enum constraint for 'id'
    so the model is nudged to only emit known item ids. Non-fatal on structure drift.
    """
    if not rf or not isinstance(rf, dict):
        return rf
    try:
        rf2 = copy.deepcopy(rf)
        if rf2.get("type") != "json_schema":
            return rf2
        js = rf2.get("json_schema") or rf2.get("schema") or {}
        schema = js.get("schema") if "schema" in js else js
        props = schema["properties"]["items"]["items"]["properties"]
        id_prop = props.get("id", {})
        if expected_ids:
            id_prop["enum"] = list(expected_ids)
            props["id"] = id_prop
        return rf2
    except Exception:
        return rf


@dataclass
class Engine:
    cfg: PatchLoopConfig

    def _split_batches_to_fit_output(self, batches: List[List[Dict]]) -> List[List[Dict]]:
        """
        Ensure each batch's estimated output fits within cfg.max_output_tokens.

        Conservative estimate:
            response_tokens_per_item * items + batch_overhead_tokens.
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

        log.info(
            f"Flags: scan={self.cfg.run_scan} "
            f"fetch={self.cfg.run_fetch_targets} "
            f"build={self.cfg.run_build_prompts} "
            f"run_llm={self.cfg.run_run_llm} "
            f"save_patch={self.cfg.run_save_patch} "
            f"sandbox={self.cfg.run_apply_patch_sandbox} "
            f"verify={self.cfg.run_verify} "
            f"archive={self.cfg.run_archive_and_replace} "
            f"rollback={self.cfg.run_rollback} "
            f"verbose={self.cfg.verbose} "
            f"max_rows={self.cfg.max_rows}"
        )

        # --- Prepare run directories
        run_dirs = RunDirs(self.cfg.out_base)
        run_id = run_dirs.make_run_id(self.cfg.run_id_suffix)
        dirs = run_dirs.ensure(run_id)
        log.stage("", f"Run directory: {dirs.root}")

        file_ops = FileOps(FileOpsConfig(preserve_crlf=self.cfg.preserve_crlf))
        out = OutputWriter(dirs.root)
        patch_ops = PatchOps(file_ops)

        # --- Step 1: external scanner (no-op placeholder)
        if self.cfg.run_scan:
            log.stage("", "Step 1 Scan: external scanner not integrated here — skipping (no-op).")

        # --- Step 2: read from DB and enrich with code
        if not self.cfg.run_fetch_targets:
            log.stage("⏭️", "Step 2 Fetch Targets: skipped by flag.")
            suspects: List[Dict] = []
        else:
            log.stage(
                "️",
                f"Step 2 Fetch Targets: reading rows from {self.cfg.sqlalchemy_url} / {self.cfg.sqlalchemy_table}",
            )
            src = IntrospectionDbSource(
                url=self.cfg.sqlalchemy_url,
                table=self.cfg.sqlalchemy_table,
                status_filter=self.cfg.status_filter,
                max_rows=self.cfg.max_rows,
            )
            retriever = SourceRetriever(
                project_root=self.cfg.project_root,
                file_ops=file_ops,
                scan_root=self.cfg.get_scan_root(),
                exclude_globs=self.cfg.scan_exclude_globs,
            )
            suspects = []
            for row in src.read_rows():
                try:
                    suspects.append(retriever.enrich(row))
                except IoError as e:
                    log.warn(f"Skipping row id={row.get('id')}: {e}")

            log.info(f"Loaded {len(suspects)} suspect(s) from DB after filtering by scan_root/excludes")

        if not suspects:
            log.warn("No suspects to process. Exiting early.")
            return dirs.root

        # --- Select task adapter (docstrings today; pluggable later)
        adapter = select_task_adapter(self.cfg.ask_spec)
        log.info(f"Task adapter: {adapter.name}")

        # --- Step 3: build prompts (context + packing)
        if not self.cfg.run_build_prompts:
            log.stage("⏭️", "Step 3 Build Prompts: skipped by flag.")
            batches: List[List[Dict]] = []
            packed_prompts: List[Dict] = []
        else:
            log.stage("", "Step 3 Build Prompts: building items and batching")
            items = adapter.build_items(suspects)
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
            batches = self._split_batches_to_fit_output(batches)
            packed_prompts = [packer.build(batch) for batch in batches]
            log.info(f"Prepared {len(packed_prompts)} batch(es)")

            for i, pb in enumerate(packed_prompts, 1):
                prompt_path = dirs.root / "raw_prompts" / f"batch_{i:03d}.txt"
                text = pb["messages"]["system"] + "\n\n" + pb["messages"]["user"]
                file_ops.write_text(prompt_path, text)
                if self.cfg.verbose:
                    log.stage("", f"Prompt {i} saved: {prompt_path}")

        # --- Step 4: call LLM (parameterised via AskSpec)
        client = LlmClient(provider=self.cfg.provider)
        try:
            route = select_route(self.cfg.ask_spec, self.cfg)
            log.info(
                f"LLM Route: ask_type={self.cfg.ask_spec.ask_type.value}, "
                f"profile={self.cfg.ask_spec.profile}, model={route.model}, "
                f"temp={route.temperature}, max_out={route.max_output_tokens}, "
                f"rf={'json_schema' if (route.response_format or {}).get('type') == 'json_schema' else 'none'}"
            )
        except AskSpecError as e:
            log.error(f"Routing failed: {e}")
            return dirs.root

        all_results: Dict[str, Dict] = {}
        for i, bundle in enumerate(packed_prompts, 1):
            if not self.cfg.run_run_llm:
                log.stage("⏭️", f"Step 4 Run LLM: skipped by flag for batch {i}.")
                continue

            per_item = int(getattr(self.cfg, "response_tokens_per_item", 320))
            overhead = int(getattr(self.cfg, "batch_overhead_tokens", 64))
            est_out = per_item * len(bundle["batch"]) + overhead
            req_max = max(min(route.max_output_tokens, est_out + 128), 256)

            expected_ids = bundle.get("ids", [])
            rf = _tighten_response_format_with_ids(route.response_format, expected_ids)

            req = LlmRequest(
                system=bundle["messages"]["system"],
                user=bundle["messages"]["user"],
                model=route.model,
                max_output_tokens=req_max,
                api_key=self.cfg.api_key,
                response_format=rf,
                temperature=route.temperature,
            )

            log.stage("", f"Batch {i}: sending to model '{route.model}' via provider '{self.cfg.provider}'")
            try:
                raw = client.complete(req)
            except (LlmClientError, Exception) as e:
                log.error(f"LLM call failed for batch {i}: {e}")
                continue

            resp_path = dirs.root / "raw_responses" / f"batch_{i:03d}.json"
            file_ops.write_text(resp_path, raw)

            try:
                parsed = adapter.parse_response(raw, expected_ids=expected_ids)
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

        # --- Steps 5–7: accumulate edits per file, then write patches/apply once per file
        by_file: Dict[str, List[Dict]] = defaultdict(list)
        for rid, result in all_results.items():
            it = item_by_id.get(rid)
            if not it:
                log.warn(f"Result id not found in items: {rid}")
                continue
            by_file[it["relpath"]].append(
                {
                    "id": rid,
                    "relpath": it["relpath"],
                    "path": Path(it["path"]),
                    "signature": it.get("signature"),
                    "target_lineno": int(it.get("target_lineno") or 0),
                    "payload": result.get("payload"),
                }
            )

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

            # per-item patches and aggregate into aggregated_src
            for e in entries_sorted:
                rid = e["id"]
                item = {
                    "id": rid,
                    "relpath": e["relpath"],
                    "path": str(path),
                    "signature": e.get("signature"),
                    "target_lineno": e["target_lineno"],
                }

                sanitized = adapter.sanitize(e["payload"], item)
                item_src = adapter.apply(original_src, item, sanitized)

                # per-item patch (against ORIGINAL)
                if self.cfg.run_save_patch and getattr(self.cfg, "save_per_item_patches", True):
                    rel_sanitized = relpath.replace("\\", "__").replace("/", "__")
                    item_patch = patch_ops.write_patch(
                        dirs.root,
                        rel_sanitized,
                        original_src,
                        item_src,
                        f"{relpath}#{rid}",
                        per_item_suffix=f"__{rid}",
                    )
                    out.append_summary(rid, relpath, item.get("signature"), str(item_patch), True, "")
                    log.stage("", f"Per-item patch written: {item_patch}")

                # apply into aggregated result
                aggregated_src = adapter.apply(aggregated_src, item, sanitized)

            # combined patch (optional)
            if self.cfg.run_save_patch and getattr(self.cfg, "save_combined_patch", True):
                rel_sanitized = relpath.replace("\\", "__").replace("/", "__")
                combined_patch = patch_ops.write_patch(
                    dirs.root, rel_sanitized, original_src, aggregated_src, relpath
                )
                log.stage("", f"Combined patch written: {combined_patch}")

            # apply once to sandbox
            if self.cfg.run_apply_patch_sandbox:
                applied = patch_ops.apply_to_sandbox(dirs.root, relpath, aggregated_src)
                log.stage("", f"Sandbox applied: {applied}")

            # verify each item's payload
            if self.cfg.run_verify:
                for e in entries_sorted:
                    rid = e["id"]
                    item = {
                        "id": rid,
                        "relpath": e["relpath"],
                        "path": str(path),
                        "signature": e.get("signature"),
                        "target_lineno": e["target_lineno"],
                    }
                    sanitized = adapter.sanitize(e["payload"], item)
                    ok, issues = adapter.verify(sanitized, item)

                    rep = dirs.root / "verify_reports" / f"{rid}.txt"
                    rep.parent.mkdir(parents=True, exist_ok=True)
                    lines = [
                        f"FILE: {relpath}",
                        f"ID: {rid}",
                        f"SIGNATURE: {item.get('signature')}",
                        "RESULT: " + ("OK" if ok else "FAIL"),
                    ]
                    if issues:
                        lines.append("ISSUES:")
                        lines.extend([f" - {x}" for x in issues])
                    else:
                        lines.append("ISSUES: (none)")

                    file_ops.write_text(rep, "\n".join(lines))
                    log.stage("", f"Verify report: {rep}")

        # Step 8: archive & replace (guarded)
        if self.cfg.run_archive_and_replace:
            if not self.cfg.confirm_prod_writes:
                log.warn("Step 8 Archive/Replace requested but confirm_prod_writes=False; skipping.")
            else:
                for relpath, entries in by_file.items():
                    path = entries[0]["path"]
                    try:
                        original_src = file_ops.read_text(path)
                    except Exception as e:
                        log.error(f"Archive/Replace: failed to read source for {relpath}: {e}")
                        continue

                    entries_sorted = sorted(entries, key=lambda x: x["target_lineno"], reverse=True)
                    aggregated_src = original_src
                    for e in entries_sorted:
                        item = {
                            "id": e["id"],
                            "relpath": e["relpath"],
                            "path": str(path),
                            "signature": e.get("signature"),
                            "target_lineno": e["target_lineno"],
                        }
                        sanitized = adapter.sanitize(e["payload"], item)
                        aggregated_src = adapter.apply(aggregated_src, item, sanitized)

                    # Archive original
                    arc_path = dirs.root / "archives" / relpath
                    arc_path.parent.mkdir(parents=True, exist_ok=True)
                    file_ops.write_text(arc_path, original_src, preserve_crlf=self.cfg.preserve_crlf)

                    # Replace in-place + mirror
                    try:
                        file_ops.write_text(path, aggregated_src, preserve_crlf=self.cfg.preserve_crlf)
                        prod_copy = dirs.root / "prod_applied" / relpath
                        prod_copy.parent.mkdir(parents=True, exist_ok=True)
                        file_ops.write_text(prod_copy, aggregated_src, preserve_crlf=self.cfg.preserve_crlf)
                        log.stage("", f"Archived → {arc_path}; Replaced source; Mirror → {prod_copy}")
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

