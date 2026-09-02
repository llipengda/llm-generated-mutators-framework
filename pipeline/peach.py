import os
from typing import override

from agent import AgentConfig, build_agent_graph
from config import get_fixer_enabled
from pipeline.base import BasePipeline
from pipeline.peach_steps import (
    CompilationStep,
    DatamodelGenerationSteps,
    DatamodelValidationSteps,
    FixerSteps,
    MutatorSteps,
    ProtocolDiscoverySteps,
)
from pipeline.peach_steps.common import _env_float


class PeachPipeline(
    ProtocolDiscoverySteps,
    DatamodelGenerationSteps,
    DatamodelValidationSteps,
    MutatorSteps,
    FixerSteps,
    CompilationStep,
    BasePipeline,
):
    def __init__(self):
        super().__init__()
        state_changed = False
        if self.state.get("peach_step_layout") != "combined-dsl-plan-v2":
            previous_index = self.state.get("current_step_index", 0)
            if previous_index >= 2:
                self.state["current_step_index"] = previous_index - 1
            elif previous_index >= 1:
                self.state["current_step_index"] = 1
            self.state["peach_step_layout"] = "combined-dsl-plan-v2"
            state_changed = True
        if (
            self.state.get("current_step_index", 0) >= 2
            and (
                self.state.get("datamodel_format") != "peach-dsl-v1"
                or "unsupported_types"
                not in self.state.get("data_type_analysis", {})
            )
        ):
            # Runs created before combined planning must regenerate their editable
            # DSL and prefixed ExtendedType contract before validation or codegen.
            self.state["current_step_index"] = 1
            state_changed = True
        if state_changed:
            self.save_state()
        peach_model = os.environ.get("LLM_PEACH_MODEL") or os.environ.get("LLM_MODEL") or "gpt-5.4"
        self.agent_config = AgentConfig(
            temperature=_env_float("LLM_PEACH_TEMPERATURE", _env_float("LLM_TEMPERATURE", 0.7)),
            model=peach_model,
            system_prompt="You are a helpful assistant expert in C# programming, protocol fuzzing and Peach Fuzzer.",
        )
        self.agent_graph = build_agent_graph(
            retriever=self.retriever, target="peach", config=self.agent_config
        )
        self.diagnosis_agent_config = AgentConfig(
            temperature=0.0,
            model=os.environ.get("LLM_DIAGNOSER_MODEL") or peach_model,
            system_prompt=(
                "You are an expert in binary protocol parsing and Peach Pit "
                "DataModels. Read the DataModel and validator logs, identify a "
                "all distinct actionable root causes supported by the evidence, "
                "and use RFC_Search only "
                "when protocol semantics need confirmation. Treat seeds as "
                "failure evidence, never as the protocol's complete grammar. "
                "Write only the "
                "requested diagnosis JSON report; never modify other files. "
                "You may read root.py as host-generated assembly context, but "
                "it is strictly read-only and must never be written, modified, "
                "patched, validated, or proposed as an editable location."
            ),
        )
        self.datamodel_autofix_agent_config = AgentConfig(
            temperature=self.agent_config.temperature,
            model=peach_model,
            system_prompt=(
                "You repair Peach DSL DataModels from a completed diagnosis "
                "report. Use Apply_Patch for localized repairs, and when the "
                "report explicitly "
                "diagnoses a missing RFC packet type, add its manifest contract "
                "and family DSL module. Consult the RFC only to model such a new "
                "packet type; never inspect validator output or failure logs. "
                "Never edit root.py because it is a derived host-generated file. "
                "Never special-case seed values or narrow the RFC-valid input "
                "space."
            ),
        )

    @override
    def steps(self):
        steps = [
            ("Step 1: Packet Types Extraction", self.step_1_packet_types_extraction),
            ("Step 2: Datamodel Generation", self.step_2_datamodel_generation),
            (
                "Step 3: Datamodel Validation & Fix",
                self.step_3_datamodel_validation_and_fix,
            ),
            ("Step 4: Mutator Generation", self.step_4_mutator_generation),
            ("Step 5: Mutator Validation & Fix", self.step_5_mutator_validation_and_fix),
        ]

        if get_fixer_enabled():
            steps += [
                ("Step 6: Constraint Extraction", self.step_6_constraint_extraction),
                ("Step 6.1: Constraint Filtering", self.step_6_1_constraint_filtering),
                ("Step 7: Fixer Generation", self.step_7_fixer_generation),
                ("Step 7.5: Fixer-Constraint Mapping", self.step_7_5_fixer_constraint_mapping),
                ("Step 8: Fixer Test Generation", self.step_8_fixer_test_generation),
                ("Step 9: Fixer Validation & Fix", self.step_9_fixer_validation_and_fix),
            ]

        steps.append(
            ("Final Compilation", self.step_final_compile),
        )

        return steps
