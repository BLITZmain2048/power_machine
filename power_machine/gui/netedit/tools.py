"""Stable drawing tool identities and explicit, incomplete creation templates."""
from .forms import creation_template

TOOLS={
    'bus':('buses','ElmTerm',0), 'line':('branches','ElmLne',2),
    'transformer2':('branches','ElmTr2',2), 'transformer3':('branches3','ElmTr3',3),
    'external':('gens','ElmXnet',1), 'load':('loads','ElmLod',1), 'synchronous':('gens','ElmSym',1),
    'static':('gens','ElmGenstat',1), 'shunt':('shunts','ElmShnt',1),
    'switch':('switches','ElmCoup',2),
}


def drawing_template(tool):
    group,cls,_=TOOLS[tool]
    if cls=='ElmXnet':
        return dict(name='',**{'class':cls},outserv=0,bus=None,p_mw=0,q_mvar=0,
                    mode='constv',ref=True,v_set_pu=None,
                    params={'bustp':'SL','usetp':None,'phiini':None,'pgini':0,'qgini':0,'snss':None,'rntxn':None,'snssmin':None,'rntxnmin':None},
                    units={'p_mw':'MW','q_mvar':'Mvar','v_set_pu':'p.u.','usetp':'p.u.','phiini':'deg','pgini':'MW','qgini':'Mvar','snss':'MVA','snssmin':'MVA','rntxn':'1','rntxnmin':'1'},type_params={},type_units={})
    if group=='branches3':
        parameters={prefix+end:None for prefix in ('strn3_','utrn3_','uktr3_','pcut3_') for end in 'hml'}
        parameters.update({k:None for k in ('nt3ag_h','nt3ag_m','nt3ag_l','curm3','pfe')})
        units={prefix+end:unit for prefix,unit in [('strn3_','MVA'),('utrn3_','kV'),('uktr3_','%'),('pcut3_','kW')] for end in 'hml'}
        units.update(nt3ag_h='1',nt3ag_m='1',nt3ag_l='1',curm3='%',pfe='kW')
        return dict(name='',**{'class':cls},outserv=0,bus_hv=None,bus_mv=None,bus_lv=None,
                    params={},units={},type_params=parameters,type_units=units)
    data=creation_template(group,cls)
    data['name']=''
    # Retain only dimensionless counts/state, never assume electrical impedance,
    # loss, admittance, power or voltage values for newly drawn equipment.
    for outer in ('params','type_params'):
        for key in data[outer]:
            if key not in ('nlnum','ntnum','ngnum','scale0'):
                data[outer][key]=None
    if group=='gens':
        data['mode']='constq'
        outer,units=('type_params','type_units') if cls=='ElmSym' else ('params','units')
        data[outer]['sgn']=None
        data[units]['sgn']='MVA'
    return data
