"""Editor DSL autónomo. Compilar y calcular no implica aceptación contra PF."""
from .schema import ModelDraft, CompileDiagnostic, CompilePreview, PreviewCase
from .compiler_adapter import compile_draft, block_view, coverage_catalog
from .preview import simulate_preview, compare_interpreter
from .workspace import (new_draft, revise_draft, import_draft, save_draft, load_draft,
    new_workspace, add_revision, save_workspace, load_workspace, controller_templates, template_draft, compose_chain)
from .ports import propose_binding, prepare_binding, instantiate_controller, check_connection
from .results import execute_request, save_result
