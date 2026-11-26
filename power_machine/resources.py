"""Bundled resources and explicit user paths."""
from __future__ import annotations
import os
from pathlib import Path
RESOURCE_ROOT_ENV = "POWER_MACHINE_RESOURCE_ROOT"
SOURCE_ROOT_ENV = "POWER_MACHINE_SOURCE_ROOT"
WORK_ROOT_ENV = "POWER_MACHINE_USER_WORK_ROOT"
RESULTS_ROOT_ENV = "POWER_MACHINE_OUTPUT_DIR"
def _resolved(value):
    return Path(value).expanduser().resolve()
def resource_root(root=None):
    """Return the package directory containing cases/."""
    return _resolved(root or os.environ.get(RESOURCE_ROOT_ENV) or Path(__file__).parent)
def readonly_path(*parts, root=None):
    """Resolve a resource confined to the package directory."""
    if len(parts) >= 2 and parts[:2] == ("cases", "ieee39_pf"):
        return ExternalPFPath(*parts[2:])
    relative = Path(*parts)
    base = resource_root(root)
    target = (base / relative).resolve()
    if not parts or relative.is_absolute() or not target.is_relative_to(base):
        raise ValueError("Resource path must be relative and confined")
    return target
def required_readonly_path(*parts, root=None):
    target = readonly_path(*parts, root=root)
    if isinstance(target, ExternalPFPath):
        return target
    if not target.exists():
        raise FileNotFoundError("Resource unavailable: " + str(target))
    return target
def model_path(root=None):
    return required_readonly_path("model.json", root=root) if root is not None else ieee39_root() / "model.json"
def overrides_path(root=None):
    return required_readonly_path("overrides.json", root=root)
def dynamic_catalog_root(root=None):
    return required_readonly_path("pf_dynamic", root=root)
def pf_reference_root(root=None):
    return required_readonly_path("pf_reference", root=root)
def ieee39_root(root=None):
    return required_readonly_path("cases", "ieee39", root=root)
def source_package_root(source_package, root=None):
    return required_readonly_path(source_package, root=root or os.environ.get(SOURCE_ROOT_ENV))
def portable_csv_root(source_package, scope, root=None):
    target = source_package_root(source_package, root) / "04_csv_portable" / scope
    if not (target / "objects.csv").is_file():
        raise FileNotFoundError("Incomplete portable CSV: " + str(target))
    return target
def confined_output(path, root=None):
    base = _resolved(root) if root is not None else output_root()
    target = _resolved(path)
    if not target.is_relative_to(base):
        raise ValueError("Output must remain within the working directory")
    return target
def output_root():
    """Resolve the default destination without creating directories."""
    explicit = os.environ.get(RESULTS_ROOT_ENV)
    if explicit:
        return _resolved(explicit)
    current = Path.home() / "power_machine_results"
    legacy = Path.home() / "power_machine_resultados"
    return _resolved(legacy if legacy.is_dir() and not current.exists() else current)

def writable_root(root=None):
    """Create the explicit destination or user results directory."""
    target = _resolved(root) if root is not None else output_root()
    target.mkdir(parents=True, exist_ok=True)
    return target

def work_path(category, root=None):
    if not category or Path(category).name != category or category in (".", ".."):
        raise ValueError("Category must be a simple name")
    base = writable_root(root)
    target = confined_output(base / category, base)
    target.mkdir(parents=True, exist_ok=True)
    return target


PF_DATA_ENV = "POWER_MACHINE_PF_IEEE39"
PF_DATA_MESSAGE = "PowerFactory reference data is not distributed; supply your own export via POWER_MACHINE_PF_IEEE39"

class PFReferenceDataError(FileNotFoundError):
    code = "pf_reference_data_not_included"
    def __init__(self):
        super().__init__(self.code + ": " + PF_DATA_MESSAGE)

def pf_data_root(root=None):
    value = root if root is not None else os.environ.get(PF_DATA_ENV)
    if not value:
        raise PFReferenceDataError()
    return _resolved(value)

def pf_data_available():
    value = os.environ.get(PF_DATA_ENV)
    return bool(value and _resolved(value).is_dir())

class ExternalPFPath(os.PathLike):
    """Lazy external path: importing an optional comparator never needs data."""
    def __init__(self, *parts):
        self.parts = parts
    def __fspath__(self):
        return os.fspath(pf_data_root().joinpath(*self.parts))
    def __str__(self):
        return self.__fspath__()
    def __truediv__(self, other):
        return ExternalPFPath(*self.parts, other)
    def __getattr__(self, name):
        return getattr(Path(self), name)


def pf_validation_data_root(root=None):
    """Resolve optional compressed validation exports outside the package."""
    value = root or os.environ.get("POWER_MACHINE_PF_VALIDATION")
    if value:
        return _resolved(value)
    return pf_data_root().parent / "validation_data_ieee39"
