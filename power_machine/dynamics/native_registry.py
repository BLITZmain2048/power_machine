'Native registry support for power-system RMS dynamics.'
from power_machine.dynamics.errors import coded_error as _coded_error
from .avr_ieeet1 import IEEET1AVR, AVR_PROFILES, PF_IDENTIFIED_STATUS
from .governors_ieeeg import IEEEG1Governor, IEEEG3Governor
from .load_general import GeneralLoad

CONTROLLER_MODELS = {
    "avr_IEEET1.BlkDef": IEEET1AVR,
    "gov_IEEEG1.BlkDef": IEEEG1Governor,
    "gov_IEEEG3.BlkDef": IEEEG3Governor,
}
LOAD_MODELS = {"general_load_t1": GeneralLoad}
VALIDATION_STATUS = 'Not validated against PF. Standard IEEE equations without exported BlkDef'


class NativeControllerCatalog:
    'Controller catalog built from projected controllers.json and composites.json data.'

    def __init__(self, inventory, *, avr_profile="dyn_a"):
        if avr_profile not in AVR_PROFILES:
            raise _coded_error(ValueError, 'invalid_native_registry_data', 'Unknown AVR profile: ' + str(avr_profile))
        self.avr_profile = avr_profile
        self.controllers = {r["path"]: r for r in inventory["controllers"]}
        self.composites = inventory["composites"]
        self.coverage = []

    @staticmethod
    def attrs(row):
        return {k: v["value"] for k, v in row["fields"].items()
                if v["value"] is not None}

    def bind(self, machine_path, machine):
        matches = [r for r in self.composites
                   if machine_path in self.attrs(r).get("pelm", ())]
        if len(matches) != 1:
            raise _coded_error(ValueError, 'invalid_native_registry_data', f'Expected one frame for {machine_path}, found {len(matches)} matches')
        comp = matches[0]
        if self.attrs(comp)["outserv"]:
            raise _coded_error(ValueError, 'invalid_native_registry_data', 'Out-of-service frame: ' + comp['relative_path'])
        result = {"avr": None, "governor": None}
        for path in self.attrs(comp)["pelm"]:
            if path is None or path == machine_path:
                continue
            if path not in self.controllers:
                raise _coded_error(ValueError, 'invalid_native_registry_data', 'Unknown frame member: ' + path)
            row = self.controllers[path]
            attrs = self.attrs(row)
            typ = attrs["typ_id"].rsplit("\\", 1)[-1]
            record = {k: row[k] for k in ("object_id", "relative_path")}
            record.update(type_name=typ, outserv=attrs["outserv"],
                          composite_path=comp["relative_path"])
            if attrs["outserv"]:
                record["status"] = 'Out of service, not modeled'
            else:
                cls = CONTROLLER_MODELS.get(typ)
                if cls is None:
                    raise _coded_error(ValueError, 'invalid_native_registry_data', 'Unregistered active control: ' + typ)
                slot = "avr" if cls is IEEET1AVR else "governor"
                if result[slot] is not None:
                    raise _coded_error(ValueError, 'invalid_native_registry_data', 'Duplicate frame control: ' + path)
                kwargs = {"profile": self.avr_profile} if slot == "avr" else {
                    "pgn_mw": machine.sgn_mva * float(machine.typ["cosn"])}
                result[slot] = cls(row["object_id"], row["relative_path"],
                                   attrs["params"], **kwargs)
                record["status"] = VALIDATION_STATUS
                if slot == "avr" and self.avr_profile == "pf_identificado":
                    record.update(avr_profile=self.avr_profile, status=PF_IDENTIFIED_STATUS)
            self.coverage.append(record)
        return comp, result["avr"], result["governor"]
