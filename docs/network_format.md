# Network format

`model.json` uses schema `power_machine-model-v1`. It represents a portable network with explicit provenance and normalized electrical groups. Load with `load_case(path)` and save through `Network.save(path)`.

The top-level groups are `buses`, `switches`, `branches`, `branches3`, `shunts`, `loads`, `gens`, `controllers`, `interface`, `other_elements`, and `warnings`. `meta` carries base quantities and provenance. Every element needs a stable `id`. Source identities can be retained as `object_id` and `relative_path`.

Important normalized fields include bus nominal voltage, branch endpoints and rating, transformer terminals, load MW/Mvar, generator MW/Mvar and voltage setpoint, and declared switch state. Keep units and references explicit. A syntactically valid file is not necessarily a solvable network: each requested study also needs its own electrical inputs and reference conditions.

Prefer `Network.set`, `Network.switch`, and `Network.save` over manual edits. When editing JSON directly, preserve unique IDs, valid bus references, list-valued groups, and coherent base quantities.


Includes importers and comparators for user-supplied PowerFactory exports; no PowerFactory data or results are distributed. Set `POWER_MACHINE_PF_IEEE39` to your export directory. Dependent features report `pf_reference_data_not_included` when data is absent.
