"""Synthetic analytical circuits for native PF passive elements."""
import copy
from pathlib import Path
import math
import numpy as np
import pytest
from power_machine import elements
from power_machine.loadflow import run_loadflow
from power_machine.dynamics.network import DynamicNetwork
from power_machine.shortcircuit import FaultSpec, ShortCircuitData, ShortCircuitOptions, run_shortcircuit
from test_shortcircuit_analytical import datum


def circuit(kind="ElmScap", **params):
    branch = dict(id="series", name="series", **{"class": kind}, bus_from="a", bus_to="b", params=params)
    return dict(meta={"s_base_mva":100, "f_nom_hz":50}, buses=[dict(id=b,name=b,un_kv=10) for b in "ab"],
                branches=[branch], branches3=[], switches=[], loads=[], shunts=[], warnings=[],
                gens=[dict(id="source",name="source",**{"class":"ElmSym"},bus="a",ref=True,mode="constv",v_set_pu=1,
                           params=dict(ngnum=1,pgini=0,qgini=0))])


def reactor(bus="b", **params):
    return dict(id="reactor", name="reactor", **{"class":"ElmShnt"},bus=bus,
                params=dict(shtype=1,ushnm=10,ncapa=1,ncapx=1,**params))


def tr3():
    tp={"curm3":0,"pfe":0}
    for k,u in zip("hml",(10,10,10)):
        tp.update({"strn3_"+k:100,"utrn3_"+k:u,"pcut3_"+k:0,"nt3ag_"+k:0})
    tp.update(uktr3_h=10,uktr3_m=30,uktr3_l=20)
    return dict(id="tr3",name="tr3",**{"class":"ElmTr3"},bus_hv="a",bus_mv="b",bus_lv="c",params={},type_params=tp)


def sc(model, bus):
    data=ShortCircuitData("power_machine-sc-data-v1",{}, {"source":{"kind":"external","parameters":{"z1":datum(.1+2j)}}})
    return run_shortcircuit(model,FaultSpec(bus,"3ph"),ShortCircuitOptions("iec60909","maximum"),data)


def test_capacitor_bridge_reactor_lf_and_rms():
    m=circuit(xcap=1)
    m["shunts"]=[reactor(xrea=10,rrea=0)]
    lf=run_loadflow(m)
    assert lf["buses"][1]["u_pu"] == pytest.approx(1/.9,rel=1e-9)
    net=DynamicNetwork(m)
    np.testing.assert_allclose(net.ybus,[[1j,-1j],[-1j,.9j]],atol=1e-12)


@pytest.mark.parametrize("kind,params,z",[("ElmScap",{"xcap":1},-1j),("ElmSind",{"ucn":10,"Sn":100,"uk":100,"Pcu":0},1j)])
def test_series_fault_analytical(kind,params,z):
    m=circuit(kind,**params)
    result=sc(m,"b")
    assert result.quantities["Ik_initial_sym_ka"] == pytest.approx(1.1*10/math.sqrt(3)/abs(.1+2j+z),rel=1e-12),result.diagnostics


def test_reactor_shunt_fault_analytical():
    m=circuit(xcap=1)
    m["shunts"]=[reactor(xrea=10,rrea=0)]
    z=1/(1/(.1+1j)+1/(10j))
    result=sc(m,"b")
    assert result.quantities["Ik_initial_sym_ka"] == pytest.approx(1.1*10/math.sqrt(3)/abs(z),rel=1e-12),result.diagnostics


def test_tr3_zero_arm_exact_lf_rms_sc():
    m=circuit("ElmSind",rrea=0,xrea=.1)
    m["buses"].append(dict(id="c",name="c",un_kv=10))
    m["branches"]=[dict(m["branches"][0],id="ab"),dict(m["branches"][0],id="ac",bus_to="c",params=dict(rrea=0,xrea=.2))]
    m["shunts"]=[reactor(xrea=10,rrea=0),dict(reactor(bus="c",xrea=20,rrea=0),id="reactor_c")]
    star=copy.deepcopy(m)
    star["branches"]=[]
    star["branches3"]=[tr3()]
    expected=np.array([[-15j,10j,5j],[10j,-10j,0],[5j,0,-5j]])
    np.testing.assert_allclose(elements.y_tr3(tr3(),10,10,10),expected,atol=1e-12)
    lf1,lf2=run_loadflow(m),run_loadflow(star)
    np.testing.assert_allclose([b["u_pu"] for b in lf1["buses"]],[b["u_pu"] for b in lf2["buses"]],atol=1e-12)
    np.testing.assert_allclose(DynamicNetwork(m).ybus,DynamicNetwork(star).ybus,atol=1e-12)
    assert sc(m,"b").quantities["Ik_initial_sym_ka"] == pytest.approx(sc(star,"b").quantities["Ik_initial_sym_ka"],rel=1e-12)


@pytest.mark.parametrize("kind,expected",[(0,1/(2-8j)),(1,1/(2+2j)),(2,.1j),(3,1/(1/(1/(2+2j)+.1)-10j)),(4,1/(1/(1/(2+1j)+.1)-5j))])
def test_all_shunt_circuits(kind,expected):
    p=dict(shtype=kind,ushnm=10,ncapa=1,ncapx=2,rrea=2,xrea=2,qcapn=10,rpara=10,c1=1e6/(2*math.pi*50),c2=1e6/(5*2*math.pi*50),f_nom_hz=50)
    assert elements.y_shunt({"params":p},10) == pytest.approx(expected)


def test_missing_shunt_parameter_and_line_alternatives():
    with pytest.raises(elements.MissingParameter,match="qrean"):
        elements.y_shunt(reactor(rrea=0),10)
    p=dict(R1=1,X1=1,B1=0,nlnum=1)
    with pytest.raises(elements.MissingParameter,match="TypLne.gline"):
        elements.y_line({"params":p},10,10)
    np.testing.assert_allclose(elements.y_line({"params":dict(p,dline=2),"type_params":{"gline":3}},10,10),
                               elements.y_line({"params":dict(p,G1=6)},10,10))


def test_bypass_is_exact_topology():
    m=circuit(xcap=1,bypass=1)
    net=DynamicNetwork(m)
    assert len(net.node_ids)==1
    np.testing.assert_array_equal(net.ybus,[[0j]])
    assert sc(m,"b").quantities["Ik_initial_sym_ka"] == pytest.approx(1.1*10/math.sqrt(3)/abs(.1+2j))



def test_import_recognizes_series_ports(monkeypatch):
    from types import SimpleNamespace
    from power_machine import builder
    objects = [dict(object_id=b, name=b, **{"class":"ElmTerm"}, scope="synthetic",relative_path=b) for b in "ab"]
    objects += [dict(object_id=k,name=k,**{"class":k},scope="synthetic",relative_path=k) for k in ("ElmScap","ElmSind")]
    parameters=[dict(object_id=b,attribute="uknom",value=10,unit="kV") for b in "ab"]
    ports=[dict(element_id=k,port=port,terminal_relative_path=b,status="PRESENT") for k in ("ElmScap","ElmSind") for port,b in zip(("bus1","bus2"),"ab")]
    package=SimpleNamespace(root=Path("synthetic"),sha256_inputs={},interface_map=[],by_id={o["object_id"]:o for o in objects},by_path={o["relative_path"]:o for o in objects},scopes={"synthetic":{"parameters":parameters,"references":[],"ports":ports}})
    monkeypatch.setattr(builder,"load_package",lambda *a,**kw:package)
    model=builder.build_model("synthetic",f_nom_hz=50)
    assert [(b["class"],b["bus_from"],b["bus_to"]) for b in model["branches"]]==[("ElmScap","a","b"),("ElmSind","a","b")]


def test_native_units_design_parameters_and_explicit_losses():
    cap={"class":"ElmScap","params":{"bcap":1000000},"units":{"bcap":"uS"}}
    np.testing.assert_allclose(elements.y_series(cap,10,10),[[1j,-1j],[-1j,1j]])
    loss={"class":"ElmScap","params":{"xcap":1,"rcap":.2}}
    assert elements.series_impedance(loss)==pytest.approx(.2-1j)
    assert elements.y_shunt(reactor(qrean=10,grea=0),10)==pytest.approx(-.1j)
    delta=reactor(xrea=30,rrea=0,ctech=0)
    assert elements.y_shunt(delta,10)==pytest.approx(-.1j)
    with pytest.raises(elements.MissingParameter,match="grea"):
        elements.y_shunt(reactor(xrea=10),10)


def test_zero_arm_with_ratio_phase_and_magnetization():
    t=tr3()
    t["type_params"].update(utrn3_h=20,nt3ag_h=1,curm3=1)
    a=2*np.exp(-1j*math.pi/6)
    expected=np.zeros((3,3),complex)
    for i,z in [(1,.1j),(2,.2j)]:
        c=np.zeros(3,complex)
        c[0],c[i]=-1/a,1
        expected+=np.outer(c.conjugate(),c)/z
    expected[0,0]+=-.01j/abs(a)**2
    np.testing.assert_allclose(elements.y_tr3(t,10,10,10),expected,atol=1e-12)
