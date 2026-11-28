"""Ideal fixed internal voltage behind native external-grid impedance."""
import numpy as np
import cmath, math
from power_machine.external_grid import impedance,number


class ExternalGridRMS:
    n_states=0
    n_alg=0

    def __init__(self,item,*,bus_un_kv,s_base_mva):
        self.object_id=item['id']
        self.relative_path=item.get('path','')
        self.bus_id=item['bus']
        self.s_base_mva=float(s_base_mva)
        self.z=impedance(item,bus_un_kv)/(bus_un_kv**2/self.s_base_mva)
        self.discrete_state={}
        self.emf=None
        tag=item.get('params',{}).get('tag')
        self.tag=None
        if tag is not None and not item.get('rms_fixed_voltage',False):
            self.tag=number(item,'tag','s')
            if self.tag<=0:raise ValueError('ElmXnet tag must be positive')
            # 2024 reference simulator TechRef ExternalGrid eq.38 and 75. tag=2H on snom.
            rx=number(item,'rntxn')
            snom=.2*math.hypot(1,rx)*number(item,'snss','MVA')/number(item,'cmax')
            self.acceleration_base=self.tag*snom/self.s_base_mva
            self.n_states=2
            self.omega0=2*math.pi*float(item.get('f_nom_hz',50))

    def init(self,v_bus,i_or_s_from_lf):
        v=complex(v_bus)
        if abs(v)==0:raise ValueError('ElmXnet initialization requires nonzero voltage')
        current=(complex(i_or_s_from_lf)/self.s_base_mva/v).conjugate()
        self.emf=v+self.z*current
        self.mechanical_power=(self.emf*current.conjugate()).real
        return np.array([0.,1.]) if self.n_states else np.empty(0)

    def current_injection(self,x,v):
        if self.emf is None:raise ValueError('ElmXnet must be initialized')
        emf=self.emf*cmath.exp(1j*float(x[0])) if self.n_states else self.emf
        return (emf-complex(v))/self.z

    def f(self,t,x,z,v):
        if not self.n_states:return np.empty(0)
        emf=self.emf*cmath.exp(1j*float(x[0]))
        pe=(emf*self.current_injection(x,v).conjugate()).real
        return np.array([self.omega0*(x[1]-1),(self.mechanical_power-pe)/(x[1]*self.acceleration_base)])
    def g(self,t,x,z,v):return np.empty(0)
    def events(self):return ()
