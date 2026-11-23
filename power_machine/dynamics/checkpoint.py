"""In-place transaction snapshots for opt-in RMS step rejection."""
from __future__ import annotations
from collections import deque
import types
import numpy as np

class RuntimeCheckpoint:
    """Snapshot reachable runtime objects while retaining identity and aliases.

    Restore device states, DSL memories, AST-keyed buffers, network arrays and
    solver caches in place. Functions, modules and classes are immutable roots.
    External I/O and callbacks must only be committed after an accepted step.
    """
    def __init__(self, *roots):
        self.entries=[]
        seen=set();pending=list(roots)
        skip=(types.ModuleType,type,types.FunctionType,types.MethodType,
              types.BuiltinFunctionType,types.BuiltinMethodType)
        while pending:
            obj=pending.pop()
            if id(obj) in seen or isinstance(obj,skip):continue
            seen.add(id(obj))
            if isinstance(obj,np.ndarray):
                if obj.flags.writeable:self.entries.append(('array',obj,obj.copy()))
            elif isinstance(obj,dict):
                saved=list(obj.items());self.entries.append(('dict',obj,saved))
                pending.extend(k for k,v in saved);pending.extend(v for k,v in saved)
            elif isinstance(obj,(list,deque)):
                saved=list(obj);self.entries.append(('sequence',obj,saved));pending.extend(saved)
            elif isinstance(obj,set):
                saved=obj.copy();self.entries.append(('set',obj,saved));pending.extend(saved)
            elif isinstance(obj,bytearray):self.entries.append(('bytes',obj,obj[:]))
            elif isinstance(obj,(tuple,frozenset)):pending.extend(obj)
            else:
                if hasattr(obj,'__dict__'):
                    saved=obj.__dict__.copy();self.entries.append(('object',obj,saved));pending.extend(saved.values())
                slots={}
                for cls in type(obj).__mro__:
                    names=cls.__dict__.get('__slots__',())
                    if isinstance(names,str):names=(names,)
                    for name in names:
                        if name in ('__dict__','__weakref__'):continue
                        if name.startswith('__') and not name.endswith('__'):
                            name='_'+cls.__name__.lstrip('_')+name
                        slots[name]=(hasattr(obj,name),getattr(obj,name,None))
                if slots:
                    self.entries.append(('slots',obj,slots))
                    pending.extend(value for present,value in slots.values() if present)
    def restore(self):
        """Restore the checkpoint without replacing any original object."""
        for kind,obj,saved in self.entries:
            if kind=='array':
                if obj.shape!=saved.shape:obj.resize(saved.shape,refcheck=False)
                np.copyto(obj,saved)
            elif kind=='dict':obj.clear();obj.update(saved)
            elif kind in ('sequence','set'):obj.clear();obj.extend(saved) if kind=='sequence' else obj.update(saved)
            elif kind=='bytes':obj[:]=saved
            elif kind=='slots':
                for name,(present,value) in saved.items():
                    if present:setattr(obj,name,value)
                    elif hasattr(obj,name):delattr(obj,name)
            else:obj.__dict__.clear();obj.__dict__.update(saved)
