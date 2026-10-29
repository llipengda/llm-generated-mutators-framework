from core.prompts import render_prompt
from core.log import run_logged_task
from core.agent import build_agent_graph
from pipeline.peach_steps.common import PeachStepMixin
from core.ui import UI


class FixerSteps(PeachStepMixin):
    def step_6_constraint_extraction(self) -> None:
        UI.title("Step 6: Constraint Extraction")

        prompt = render_prompt("fixers/constraints.txt", protocol_name=self.protocol_name)

        response = self.call_agent(prompt, "Step 6: Constraint Extraction")
        constraints = str(getattr(response["messages"][-1], "content"))
        self.state["constraints"] = constraints
        self.save_state()
        UI.success("Constraints extracted successfully.")

    def step_6_1_constraint_filtering(self):
        UI.title("Step 6.1: Constraint Filtering")
        constraints = self.state.get("constraints") or ""
        if not constraints:
            UI.warn(
                "Warning: constraints is empty (Step 6 may have been skipped). Step 6.1 will still run."
            )
            return
        prompt = render_prompt(
            "fixers/filter_constraints.txt",
            protocol_name=self.protocol_name,
            protocol_lower=self.protocol_lower,
            constraints=constraints,
        )

        self.call_agent(prompt, "Step 6.1: Constraint Filtering")

    def step_7_fixer_generation(self):
        UI.title("Step 7: Fixer Generation")

        constraints = ""
        with open(f"./llm/peach/{self.protocol_lower}/constraint_analysis.txt", "r", encoding="utf-8") as f:
            constraints = f.read()

        constraint_blocks = list(map(lambda x: x.replace("[NOT GUARANTEED]", ""), 
                                filter(lambda c: c.startswith("[NOT GUARANTEED]"), 
                                       [c.strip() for c in constraints.split("\n\n") if c.strip()])))
        
        # Group constraints into chunks of 2
        chunk_size = 3
        chunks = [constraint_blocks[i:i + chunk_size] for i in range(0, len(constraint_blocks), chunk_size)]

        def run_fixer_chunk(chunk: list[str], index: int):
            chunk_content = "\n\n".join(chunk)
            prompt = render_prompt(
                "fixers/generation.txt",
                protocol_lower=self.protocol_lower,
                index=index,
                protocol_upper=self.protocol_upper,
                chunk_content=chunk_content,
            )

            agent = build_agent_graph(
                retriever=self.retriever, config=self.agent_config
            )

            self.call_agent(
                prompt,
                f"Step 7.1.{index}: Fixer Generation Part {index}",
                agent_graph=agent,
            )

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(run_logged_task, f"Fixer chunk {idx + 1}", run_fixer_chunk, chunk, idx) for idx, chunk in enumerate(chunks)]
            for future in as_completed(futures):
                future.result()

        UI.title("Step 7.2: Fixup Class Generation")

        fixup_prompt = render_prompt(
            "fixers/fixup_generation.txt",
            protocol_lower=self.protocol_lower,
            protocol_upper=self.protocol_upper,
        )

        self.call_agent(fixup_prompt, "Step 7.2: Fixup Class Generation")

    def step_7_5_fixer_constraint_mapping(self):
        UI.title("Step 7.5: Fixer-Constraint Mapping")

        mapping_prompt = render_prompt("fixers/constraint_mapping.txt", protocol_lower=self.protocol_lower)

        self.call_agent(mapping_prompt, "Step 7.5: Fixer-Constraint Mapping")

    def step_8_fixer_test_generation(self):
        UI.title("Step 8: Fixer Test Generation")

        import os
        dll_source = f"./llm/peach/{self.protocol_lower}/Fixers/out/{self.protocol_upper}Fixers.dll"
        from core.peach_sdk import peach_sdk_dir

        dll_destination = str(peach_sdk_dir() / f"{self.protocol_upper}Fixers.dll")
        if os.path.exists(dll_source):
            import shutil
            shutil.copy(dll_source, dll_destination)
            UI.success(f"Copied {dll_source} to {dll_destination} for test compilation.")
        else:
            UI.warn(f"Expected DLL not found at {dll_source}. Make sure Step 7 completed successfully. Step 8 may fail to compile tests without the fixers DLL.")

        # constraints = self.state.get("constraints") or ""
        constraints = ''
        with open(f"./llm/peach/{self.protocol_lower}/Fixers/fixer_constraint_mapping.txt", "r", encoding="utf-8") as f:
            constraints = f.read()
        if not constraints:
            UI.warn("Warning: constraints is empty. Step 8 will not generate any tests.")
            return

        constraint_blocks = [c.strip() for c in constraints.split("\n\n") if c.strip()]
        
        # Group constraints into chunks of 2
        chunk_size = 3
        chunks = [constraint_blocks[i:i + chunk_size] for i in range(0, len(constraint_blocks), chunk_size)]

        def run_test_chunk(chunk: list[str], index: int):
            chunk_content = "\n\n".join(chunk)
            prompt = render_prompt(
                "fixers/test_generation.txt",
                protocol_lower=self.protocol_lower,
                protocol_upper=self.protocol_upper,
                index=index,
                chunk_content=chunk_content,
            )

            agent = build_agent_graph(
                retriever=self.retriever, config=self.agent_config
            )

            self.call_agent(
                prompt,
                f"Step 8.{index}: Fixer Validation Generation Part {index}",
                agent_graph=agent,
            )

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(run_logged_task, f"Fixer tests {idx + 1}", run_test_chunk, chunk, idx) for idx, chunk in enumerate(chunks)]
            for future in as_completed(futures):
                future.result()

    def step_9_fixer_validation_and_fix(self):
        UI.title("Step 9: Fixer Validation & Fix")

        import os
        import glob

        def verify_fn() -> tuple[bool, str]:
            cmd = [
                "./tests/peach_fixer/run_peach_fixer_test.sh",
                self.protocol_lower,
                self.seed_dir,
            ]
            UI.run_with_live_output(
                cmd, title="Running Fixer Tests"
            )

            log_dir = (
                f"./llm/peach/{self.protocol_lower}/fixer_test_logs"
            )
            all_logs = glob.glob(os.path.join(log_dir, "*.log"))
            fail_logs = [
                l for l in all_logs if os.path.basename(l) != "fixer.log"
            ]

            if not fail_logs:
                return True, ""

            parts: list[str] = []
            for log_file in fail_logs:
                with open(log_file, "r", encoding="utf-8") as f:
                    parts.append(
                        f"--- {os.path.basename(log_file)} ---\n{f.read()}"
                    )
            return False, "\n\n".join(parts)

        def fix_fn(output: str, hint: str | None) -> None:
            log_dir = (
                f"./llm/peach/{self.protocol_lower}/fixer_test_logs"
            )
            all_logs = glob.glob(os.path.join(log_dir, "*.log"))
            fail_logs = [
                l for l in all_logs if os.path.basename(l) != "fixer.log"
            ]

            if not fail_logs:
                UI.success(
                    "No fixer test failures to fix (already resolved)."
                )
                return

            UI.warn(
                f"Found {len(fail_logs)} fixer tests with failures. Attempting to fix..."
            )

            for log_file in fail_logs:
                with open(log_file, "r", encoding="utf-8") as f:
                    test_output = f.read()

                test_name = os.path.basename(log_file).replace(".log", "")

                prompt = render_prompt(
                    "fixers/test_repair.txt",
                    test_name=test_name,
                    test_output=test_output,
                    protocol_lower=self.protocol_lower,
                    protocol_upper=self.protocol_upper,
                )
                if hint:
                    prompt += (
                        render_prompt("shared/user_guidance.txt", hint=hint)
                    )

                self.call_agent(
                    prompt, f"Step 9: Fix Fixer Test {test_name}"
                )

        if not self.fix_verify_loop(
            "Step 9: Fixer Validation & Fix", verify_fn, fix_fn
        ):
            return
