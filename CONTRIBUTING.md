# Contributing to Power Machine

Thanks for contributing. Please keep changes focused, documented, and
reproducible.

## Development setup

Use Python 3.10 or later. Create and activate a virtual environment, then
install the project and test runner:

```console
python -m pip install -e .
python -m pip install pytest
```

Install GUI dependencies when working on the desktop interface:

```console
python -m pip install -e ".[gui]"
```

For the optional SciPy-accelerated path, install the `fast` extra:

```console
python -m pip install -e ".[fast]"
```

## Tests

Run the full test suite with:

```console
python -m pytest
```

Run GUI tests only on a machine with the required graphical dependencies:

```console
python -m pytest -m gui
```

When changing an accelerated path, run the relevant tests both with and
without the `fast` extra. Results must agree within the documented tolerance.

## Code and study conventions

Follow the existing Python style, use clear names, and add focused tests for
new behavior. State units explicitly in public parameters, result columns,
documentation, and data fields, for example `t_end_s`, `voltage_kv`, and
`power_mw`.

Do not describe a study or capability as *validated* unless a documented
comparison against a stated reference supports that claim. Record the
reference, inputs, commands, tolerances, outputs, and any known limitations.

## References and case data

Add a provenance note for every new reference dataset or derived case. It must
identify the source, version or revision, applicable terms, transformations,
units, and file paths. Update `NOTICE` whenever the included third-party data
or reference exports change. Do not assume that a code license applies to
case data.


Includes importers and comparators for user-supplied PowerFactory exports; no PowerFactory data or results are distributed. Set `POWER_MACHINE_PF_IEEE39` to your export directory. Dependent features report `pf_reference_data_not_included` when data is absent.
