import os
from typing import override

from core.prompts import render_prompt
from core.agent import AgentConfig, build_agent_graph
from core.config import get_language
from pipeline.base import BasePipeline
from pipeline.peach_steps import (
    CompilationStep,
    DatamodelGenerationSteps,
    DatamodelValidationSteps,
    MutatorSteps,
    ProtocolDiscoverySteps,
)
from pipeline.peach_steps.common import env_float


class PeachPipeline(
    ProtocolDiscoverySteps,
    DatamodelGenerationSteps,
    DatamodelValidationSteps,
    MutatorSteps,
    CompilationStep,
    BasePipeline,
):
    def __init__(self):
        super().__init__()
        state_changed = False
        if self.state.get("peach_step_layout") not in {
            "combined-dsl-plan-v2", "combined-dsl-mutators-v3",
        }:
            previous_index = self.state.get("current_step_index", 0)
            if previous_index >= 2:
                self.state["current_step_index"] = previous_index - 1
            elif previous_index >= 1:
                self.state["current_step_index"] = 1
            self.state["peach_step_layout"] = "combined-dsl-plan-v2"
            state_changed = True
        if self.state.get("peach_step_layout") != "combined-dsl-mutators-v3":
            # Old runs can point into removed steps or beyond the new step list.
            # Resume these runs at final compilation with the retained sources.
            self.state["current_step_index"] = min(self.state.get("current_step_index", 0), 5)
            self.state["peach_step_layout"] = "combined-dsl-mutators-v3"
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
            language=get_language(),
            temperature=env_float("LLM_PEACH_TEMPERATURE", env_float("LLM_TEMPERATURE", 0.7)),
            model=peach_model,
            system_prompt=render_prompt("system/peach.txt"),
        )
        self.agent_graph = build_agent_graph(
            retriever=self.retriever, config=self.agent_config
        )
        self.diagnosis_agent_config = AgentConfig(
            language=get_language(),
            temperature=0.0,
            model=os.environ.get("LLM_DIAGNOSER_MODEL") or peach_model,
            system_prompt=render_prompt("system/datamodel_diagnosis.txt"),
        )
        self.datamodel_autofix_agent_config = AgentConfig(
            language=get_language(),
            temperature=self.agent_config.temperature,
            model=peach_model,
            system_prompt=render_prompt("system/datamodel_repair.txt"),
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

        steps.append(
            ("Final Compilation", self.step_final_compile),
        )

        return steps
