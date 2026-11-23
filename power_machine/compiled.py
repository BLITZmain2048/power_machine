"""Reusable network snapshots, copy-on-write scenarios and optional sparse algebra."""
from __future__ import annotations
import copy
import hashlib
import json
from collections import OrderedDict
from functools import lru_cache
import numpy as np

_GROUPS = ('buses','switches','branches','branches3','shunts','loads','gens')
_INJECTIONS = {'load_scale','load_p_mw','load_q_mvar','gen_p_mw','gen_q_mvar','v_set_pu'}

def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
        default=lambda x: x.tolist() if hasattr(x,'tolist') else x.item() if hasattr(x,'item') else str(x), allow_nan=False).encode()).hexdigest()

@lru_cache(maxsize=1)
def _scipy():
    try:
        from scipy.sparse import csr_matrix
        from scipy.sparse.linalg import splu
        return csr_matrix, splu
    except ImportError:
        return None

def select_backend(backend, size):
    """Select dense or sparse algebra without importing SciPy for small auto cases."""
    if backend not in ('dense','sparse','auto'):
        raise ValueError('[invalid_backend] backend must be dense, sparse or auto')
    wanted = backend == 'sparse' or (backend == 'auto' and size >= 200)
    available = _scipy() if wanted else None
    return ('sparse' if available else 'dense'), (wanted and available is None)

def scenario_view(model, scenario=None, index=None):
    """Apply deltas by copying changed records only. Unchanged records are read-only."""
    if not scenario:
        return model
    index = index or {v['id']:(g,i,v) for g in _GROUPS for i,v in enumerate(model.get(g,()))}
    result = dict(model)
    changed = {}
    for field, changes in sorted(scenario.items(), key=lambda pair: 2 if pair[0] in {"load_p_mw","load_q_mvar","gen_p_mw","gen_q_mvar"} else 1 if pair[0] in {"load_scale","v_set_pu"} else 0):
        if field not in _INJECTIONS | {'outserv','closed','ref','tap_pos','params'}:
            raise ValueError('[unsupported_scenario_field] Unsupported scenario field: '+str(field))
        if not isinstance(changes,dict):
            raise ValueError('[invalid_scenario] Scenario changes must be mappings')
        for ident,value in changes.items():
            if ident not in index:
                raise ValueError('[unknown_scenario_element] Unknown scenario element: '+str(ident))
            group,pos,original=index[ident]
            if ident not in changed:
                if result[group] is model[group]:result[group]=list(model[group])
                item=dict(original)
                item['params']=dict(original.get('params',{}))
                result[group][pos]=item
                changed[ident]=item
            item=changed[ident]
            if field=='outserv':item[field]=int(value)
            elif field=='closed':item[field]=bool(value)
            elif field=='ref':
                if group != 'gens' or value not in (0,1,False,True):
                    raise ValueError('[invalid_reference_delta] Reference changes require generator IDs and booleans')
                item['ref']=bool(value)
            elif field=='params':
                if not isinstance(value,dict):raise ValueError('[invalid_scenario_parameters] params must be a mapping')
                item['params'].update(value)
            elif field=='tap_pos':
                if item['class'] not in ('ElmTr2','ElmTr3'):raise ValueError('[invalid_tap_target] Tap changes require a transformer')
                if item['class']=='ElmTr3':
                    if not isinstance(value,dict):
                        raise ValueError('[invalid_tap_delta] Three-winding tap_pos must map hv, mv or lv positions')
                    labels={'hv':'h','mv':'m','lv':'l','h':'h','m':'m','l':'l'}
                    for winding,position in value.items():
                        if winding not in labels:
                            raise ValueError('[invalid_tap_winding] Unknown three-winding tap terminal: '+str(winding))
                        item['params']['n3tap_'+labels[winding]]=float(position)
                else:
                    item['params']['nntap']=float(value)
            elif field=='load_scale':
                if item['class']!='ElmLod':raise ValueError('[invalid_load_target] Element is not ElmLod: '+ident)
                item['params']['scale0']=float(value)
                item['scale']=float(value)
            elif field in ('load_p_mw','load_q_mvar'):
                if item['class']!='ElmLod':raise ValueError('[invalid_load_target] Element is not ElmLod: '+ident)
                scale=float(item['params'].get('scale0',1))
                key='plini' if field=='load_p_mw' else 'qlini'
                if scale == 0:
                    if float(value) != 0:raise ValueError('[zero_load_scale] Nonzero total power requires nonzero load scale')
                    item['params'][key]=0.0
                else:item['params'][key]=float(value)/scale
            elif field=='v_set_pu':
                if group!='gens':raise ValueError('[invalid_generator_target] Element is not a generator: '+ident)
                item['v_set_pu']=float(value)
                item['params']['usetp']=float(value)
            else:
                if group!='gens':raise ValueError('[invalid_generator_target] Element is not a generator: '+ident)
                p=item['params']
                multiplier=float(p.get('ngnum',1))*float(p.get('scale0',1))
                divisor=1000 if item['class']=='ElmPvsys' else 1
                key='pgini' if field=='gen_p_mw' else 'qgini'
                if multiplier == 0:
                    if float(value) != 0:raise ValueError('[zero_generator_multiplier] Nonzero total power requires nonzero generator multiplier')
                    p[key]=0.0
                else:p[key]=float(value)*divisor/multiplier
                item['p_mw' if field=='gen_p_mw' else 'q_mvar']=float(value)
    return result

def clone_topology(topology):
    """Copy result-facing topology containers without copying model records."""
    return {**topology,'bus_to_node':dict(topology['bus_to_node']),
        'islands':[{**i,'nodes':list(i['nodes']),'buses':list(i['buses']),
                    'elements':{g:list(ids) for g,ids in i['elements'].items()}} for i in topology['islands']]}

def assemble_ybus(topology, stamps, items, buses, island, s_base, backend, f_nom_hz=None):
    """Assemble CSR directly from electrical stamps, or assemble the dense reference."""
    from . import elements
    nodes=island['nodes']; index={node:i for i,node in enumerate(nodes)}
    b2n=topology['bus_to_node']
    rows=[{i:0j} for i in range(len(nodes))]
    def add(i,j,value):rows[i][j]=rows[i].get(j,0j)+value
    for group in ('branches','branches3'):
        for ident in island['elements'][group]:
            ids,_,matrix=stamps[ident]
            indices=[index[b2n[b]] for b in ids]
            for i in set(indices):
                for j in set(indices):
                    add(i,j,sum(matrix[a,b] for a,ia in enumerate(indices)
                                for b,jb in enumerate(indices) if ia==i and jb==j))
    for ident,connected in topology['dangling_branches'].items():
        item=items[ident]; bus=item['bus_from'] if connected==0 else item['bus_to']
        if topology['node_to_island'][b2n[bus]]!=island['id']:continue
        matrix=stamps[ident][2]; other=1-connected
        value=matrix[connected,connected]-matrix[connected,other]*matrix[other,connected]/matrix[other,other]
        add(index[b2n[bus]],index[b2n[bus]],value)
    for ident in island['elements']['shunts']:
        item=items[ident]; i=index[b2n[item['bus']]]
        add(i,i,elements.y_shunt(item,buses[item['bus']]['un_kv'],s_base,f_nom_hz=f_nom_hz))
    if backend=='sparse':
        data=[];indices=[];indptr=[0]
        for row in rows:
            for j,value in sorted(row.items()):indices.append(j);data.append(value)
            indptr.append(len(data))
        y=_scipy()[0]((np.asarray(data,complex),np.asarray(indices),np.asarray(indptr)),shape=(len(nodes),len(nodes)))
    else:
        y=np.zeros((len(nodes),len(nodes)),complex)
        for i,row in enumerate(rows):
            for j,value in row.items():y[i,j]=value
        y.flags.writeable=False
    members={node:[] for node in nodes}
    for bus in island['buses']:members[b2n[bus]].append(bus)
    return {'node_index':index,'ybus':y,'patterns':{},'members':members}

class SparseJacobianPattern:
    """Fixed CSR Jacobian structure for one Ybus pattern and one PV/PQ regime."""
    def __init__(self,ybus,angle_idx,pq_idx):
        n=ybus.shape[0]
        self.rows=np.repeat(np.arange(n),np.diff(ybus.indptr))
        self.cols=ybus.indices
        amap=np.full(n,-1,int);pmap=np.full(n,-1,int)
        amap[angle_idx]=np.arange(len(angle_idx));pmap[pq_idx]=np.arange(len(pq_idx))
        rr=[];cc=[];kk=[];yy=[];offset=len(angle_idx)
        for kind,(rmap,cmap,ro,co) in enumerate(((amap,amap,0,0),(amap,pmap,0,offset),(pmap,amap,offset,0),(pmap,pmap,offset,offset))):
            valid=(rmap[self.rows]>=0)&(cmap[self.cols]>=0)
            positions=np.flatnonzero(valid)
            rr.extend((rmap[self.rows[positions]]+ro).tolist());cc.extend((cmap[self.cols[positions]]+co).tolist())
            kk.extend([kind]*len(positions));yy.extend(positions.tolist())
        self.size=offset+len(pq_idx)
        order=np.argsort(np.asarray(rr,dtype=np.int64)*max(1,self.size)+np.asarray(cc,dtype=np.int64),kind='stable')
        self.indices=np.asarray(cc,dtype=int)[order];ordered_rows=np.asarray(rr,dtype=int)[order]
        self.indptr=np.r_[0,np.cumsum(np.bincount(ordered_rows,minlength=self.size))]
        self.kind=np.asarray(kk,dtype=int)[order];self.ypos=np.asarray(yy,dtype=int)[order]
    def matrix(self,ybus,vm,angle,power):
        i,j=self.rows,self.cols;g,b=ybus.data.real,ybus.data.imag
        delta=angle[i]-angle[j];c,s=np.cos(delta),np.sin(delta)
        vv=vm[i]*vm[j]
        h=vv*(g*s-b*c);nn=vm[i]*(g*c+b*s);m=-vv*(g*c+b*s);ell=vm[i]*(g*s-b*c)
        d=np.flatnonzero(i==j);di=i[d]
        h[d]=-power.imag[di]-b[d]*vm[di]**2
        nn[d]=power.real[di]/vm[di]+g[d]*vm[di]
        m[d]=power.real[di]-g[d]*vm[di]**2
        ell[d]=power.imag[di]/vm[di]-b[d]*vm[di]
        values=np.choose(self.kind,(h[self.ypos],nn[self.ypos],m[self.ypos],ell[self.ypos]))
        return _scipy()[0]((values,self.indices,self.indptr),shape=(self.size,self.size))

def linear_solve(matrix,rhs,backend='dense'):
    """Factor the current numerical matrix and solve. No Newton LU reuse."""
    if backend=='sparse':
        try:return _scipy()[1](matrix.tocsc()).solve(rhs)
        except RuntimeError as exc:raise np.linalg.LinAlgError(str(exc)) from exc
    return np.linalg.solve(matrix,rhs)

class CompiledNetwork:
    """Independent snapshot for repeated studies. Build once per process or worker.

    Treat model as read-only. invalidate(source_model) refreshes it after source edits.
    The source dictionary is never modified. Numerical Newton states are not cached.
    """
    def __init__(self,model,*,backend='auto'):
        self._source=model
        self.backend=backend
        self.model=copy.deepcopy(model)
        self.fingerprint=_digest(self.model)
        self.index={v['id']:(g,i,v) for g in _GROUPS for i,v in enumerate(self.model.get(g,()))}
        self._states=OrderedDict();self._stamps={};self._overrides=OrderedDict()
    def invalidate(self,model=None):
        """Refresh this snapshot and invalidate topology, stamps and Jacobian patterns."""
        self.__init__(self._source if model is None else model,backend=self.backend)
        return self
    def run_loadflow(self,scenario=None,options=None):
        """Solve one independent scenario using this network preparation."""
        from .loadflow import run_loadflow
        return run_loadflow(self,scenario,options)
    def scenario(self,scenario=None):
        """Return a read-only copy-on-write view of the requested operating deltas."""
        return scenario_view(self.model,scenario,self.index)
    def overridden(self,overrides):
        """Cache a separately prepared snapshot with explicit provenance overrides."""
        from .overrides import apply_overrides
        key=_digest(overrides)
        if key not in self._overrides:
            model,warnings=apply_overrides(self.model,overrides)
            self._overrides[key]=(CompiledNetwork(model,backend=self.backend),warnings)
            if len(self._overrides)>4:self._overrides.popitem(last=False)
        return self._overrides[key]
    def state(self,view,scenario,options):
        """Return internal read-only topology, stamps, matrices and Jacobian patterns."""
        from .loadflow import _prepare_state
        backend,unavailable=select_backend(options.get('backend',self.backend),len(view.get('buses',())))
        structure={k:v for k,v in (scenario or {}).items() if k not in _INJECTIONS}
        key=(_digest(structure),backend,unavailable,options.get('missing','error'),bool(options.get('provisional_tr3')))
        if key in self._states:
            self._states.move_to_end(key)
            return self._states[key]
        state=_prepare_state(view,options,self._stamps)
        state['backend']=backend
        state['backend_unavailable']=unavailable
        state['plans']={i['id']:assemble_ybus(state['topology'],state['stamps'],state['items'],state['buses'],i,state['s_base'],backend,view.get('meta',{}).get('f_nom_hz'))
                        for i in state['topology']['islands'] if i['status']!='sin_referencia'}
        self._states[key]=state
        def matrix_bytes(value):
            return value.data.nbytes+value.indices.nbytes+value.indptr.nbytes if hasattr(value,'indptr') else value.nbytes
        state['_bytes']=sum(matrix_bytes(p['ybus']) for p in state['plans'].values())
        while len(self._states)>1 and (len(self._states)>64 or sum(s['_bytes'] for s in self._states.values())>128*1024**2):
            self._states.popitem(last=False)
        return state

def compile_network(model,*,backend='auto'):
    """Prepare an independent model snapshot for repeated serial or worker-local solves."""
    if isinstance(model,CompiledNetwork):return model
    return CompiledNetwork(model,backend=backend)

_CACHE=OrderedDict()
def cached_network(model):
    """Reuse a content-checked snapshot for a dictionary, or return a prepared object."""
    if isinstance(model,CompiledNetwork):return model
    ident=id(model);fingerprint=_digest(model)
    old=_CACHE.get(ident)
    if old is not None and old._source is model and old.fingerprint==fingerprint:
        _CACHE.move_to_end(ident)
        return old
    result=CompiledNetwork(model)
    _CACHE[ident]=result
    if len(_CACHE)>4:_CACHE.popitem(last=False)
    return result
