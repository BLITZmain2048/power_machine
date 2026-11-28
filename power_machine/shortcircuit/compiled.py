"""Study-local sequence preparation and reusable numerical factorizations."""
from collections import OrderedDict
import copy
from dataclasses import asdict
import numpy as np
from ..compiled import _digest, select_backend, _scipy
from .sequences import prepare_network, stamp_island
from .schema import SCError

class SequenceFactorization:
    """Factor once for multiple fault columns of an unchanged sequence matrix."""
    def __init__(self,y,backend):
        self.y=y;self.backend=backend
        try:
            self.factor=_scipy()[1](y.tocsc()) if backend=='sparse' else None
            # Multiple right-hand sides share one dense LAPACK factorization.
            self.columns=np.linalg.solve(y,np.eye(y.shape[0],dtype=complex)) if backend=='dense' else None
        except (np.linalg.LinAlgError,RuntimeError) as exc:
            raise SCError("singular_sequence_matrix", str(exc), status="numerical_failure") from exc
    def column(self,position):
        """Solve one fault column using the existing numerical factorization."""
        if self.columns is not None:return self.columns[:,position].copy()
        rhs=np.zeros(self.y.shape[0],complex);rhs[position]=1
        return self.factor.solve(rhs)

class ShortCircuitWorkspace:
    """Reuse network matrices only when the fault does not change station corrections.

    Input records are read-only for the workspace lifetime. Create a new workspace
    after a model, supplementary parameter or study option changes.
    """
    def __init__(self,model,data,options,kind):
        self.model,self.data,self.options,self.kind=model,data,options,kind
        self.validated = False
        self.networks={};self.matrices=OrderedDict();self.factorizations={};self.weights={}
        generators={g['id']:g.get('bus') for g in model.get('gens',())}
        self.station_buses=set()
        if options.profile=='iec_ref2016_c1_v1':
            for record in data.elements.values():
                sid=record.get('station_source_id')
                if sid is not None:
                    self.station_buses.add(data.sources.get(sid,{}).get('bus_id',generators.get(sid)))
        self.backend,self.backend_unavailable=select_backend(getattr(options,'backend','auto'),len(model.get('buses',())))
    def correction_key(self,fault_bus_id):
        """Separate internal generator-station faults from unchanged external faults."""
        return fault_bus_id if fault_bus_id in self.station_buses else None
    def network(self,fault_bus_id):
        """Return a sequence network with the correction regime required by the fault."""
        key=self.correction_key(fault_bus_id)
        if key not in self.networks:
            self.networks[key]=prepare_network(self.model,self.data,self.options,self.kind,fault_bus_id)
        return self.networks[key]
    def matrix(self,fault_bus_id,network,nodes,seq):
        """Return the matrix, terminal stamps and factorization for one regime."""
        key=(self.correction_key(fault_bus_id),tuple(nodes),seq)
        if key not in self.matrices:
            self.matrices[key]=stamp_island(network,nodes,seq,backend=self.backend)
        y,stamps=self.matrices[key]
        if key not in self.factorizations:
            self.factorizations[key]=SequenceFactorization(y,self.backend)
            self.weights[key]=(y.data.nbytes+y.indices.nbytes+y.indptr.nbytes) if self.backend=="sparse" else y.nbytes+self.factorizations[key].columns.nbytes
        self.matrices.move_to_end(key)
        while len(self.matrices)>1 and sum(self.weights.values())>128*1024**2:
            old,_=self.matrices.popitem(last=False)
            self.factorizations.pop(old,None)
            self.weights.pop(old,None)
        return y,stamps,self.factorizations[key]

_CACHE=OrderedDict()
def cached_workspace(model,data,options,kind):
    """Reuse a workspace only when model, supplement, options and fault kind match."""
    key=_digest({'model':model,'data':{'schema':data.schema,'elements':data.elements,'sources':data.sources,'assumptions':data.assumptions},'options':asdict(options),'kind':kind})
    if key not in _CACHE:
        _CACHE[key]=ShortCircuitWorkspace(copy.deepcopy(model),copy.deepcopy(data),copy.deepcopy(options),kind)
        if len(_CACHE)>4:_CACHE.popitem(last=False)
    else:_CACHE.move_to_end(key)
    return _CACHE[key]
