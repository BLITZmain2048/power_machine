"""Pruebas de reguladores, curvas sin ajuste y proyecciones de solo lectura."""
import csv
from pathlib import Path

import numpy as np
import pytest

from power_machine.benchmarks.ieee39_pf import DEFAULT_PF_DIR, WORKSPACE
from power_machine.dynamics.avr_ieeet1 import IEEET1AVR
from power_machine.dynamics.avr_exponential import ExponentialIEEET1AVR
from power_machine.dynamics.regulator_diagnostics import sparse_projection, coordinate_transform, mapped_matrix, pf_short, participation_shares
from power_machine.dynamics.native_registry import CONTROLLER_MODELS
from power_machine.pfcompare.regulator_experiment import build_variant, comparator_options, fresh_output
from power_machine.pfcompare import rms_network as rms_replay, modal_network as mod

PARAMS = [0., 5., .06, -.0198, .5, .08, 1., 2.342286, .13, 3.123048, .34, -1., 1.]


def test_exponential_passes_exported_points_without_free_parameters():
    c = ExponentialIEEET1AVR("id", "path", PARAMS)
    assert c.saturation(PARAMS[7]) == pytest.approx(PARAMS[8], rel=1e-14)
    assert c.saturation(PARAMS[9]) == pytest.approx(PARAMS[10], rel=1e-14)
    assert list(c.parameters.values()) == PARAMS
    assert CONTROLLER_MODELS["avr_IEEET1.BlkDef"] is IEEET1AVR


@pytest.mark.parametrize("efd", [.1, 1.2, 2.2318306, 4.])
def test_drop_derivative_and_complete_jacobian_by_independent_stencil(efd):
    c = ExponentialIEEET1AVR("id", "path", PARAMS)
    x,u = c.initialize({"Efd": 2.2318306, "Vt": .9831})
    x[2] = efd
    h = 1e-6
    numerical = (c._saturation_drop(efd+h)[0]-c._saturation_drop(efd-h)[0])/(2*h)
    assert c._saturation_drop(efd)[1] == pytest.approx(numerical, rel=1e-8, abs=1e-10)
    df,dy = c.jacobian(0.,x,u)
    point = np.r_[x,[u[k] for k in c.ir.inputs]]
    for j in range(len(point)):
        outputs=[]
        derivatives=[]
        for sign in (1.,-1.):
            p=point.copy()
            p[j]+=sign*h
            out,dx=c.evaluate(0.,p[:4],dict(zip(c.ir.inputs,p[4:])))
            outputs.append(out["Efd"])
            derivatives.append(dx)
        np.testing.assert_allclose(df[:,j],(derivatives[0]-derivatives[1])/(2*h),rtol=1e-7,atol=1e-7)
        assert dy[0,j] == pytest.approx((outputs[0]-outputs[1])/(2*h),abs=1e-8)


def test_frozen_auxiliary_independent_of_physical_avr_and_positive_tr():
    lag = ExponentialIEEET1AVR("id", "path", PARAMS)
    frozen = ExponentialIEEET1AVR("id", "path", PARAMS, frozen_measurement=True)
    x,u=lag.initialize({"Efd":2.2318306,"Vt":.9831})
    u["Vt"] = .9
    y,dx=lag.evaluate(0.,x,u)
    yf,dxf=frozen.evaluate(0.,x,u)
    assert dx[0]!=0. and dxf[0]==0.
    assert y==yf
    np.testing.assert_array_equal(dx[1:],dxf[1:])
    assert not frozen.jacobian(0.,x,u)[0][0].any()
    positive=PARAMS.copy()
    positive[0]=.02
    c=ExponentialIEEET1AVR("id","path",positive,frozen_measurement=True)
    assert c.evaluate(0.,x,u)[1][0] == pytest.approx((.9-.9831)/.02)


@pytest.mark.parametrize("s1,s2", [(-.1,.3),(0.,.3),(.3,.1)])
def test_invalid_exponential_points_rejected(s1,s2):
    p=PARAMS.copy()
    p[8],p[10]=s1,s2
    with pytest.raises(ValueError,match="Exponential"):
        ExponentialIEEET1AVR("id","path",p)


def test_both_zero_saturation_and_negative_field():
    p=PARAMS.copy()
    p[8]=p[10]=0.
    c=ExponentialIEEET1AVR("id","path",p)
    assert c._saturation_drop(2.)==(0.,0.)
    assert c._saturation_drop(-2.)==(0.,0.)


def test_sparse_projection_ignores_unrequested_and_rejects_blank(tmp_path):
    path=tmp_path/"source.csv"
    path.write_text("row,column,real,imag\n1,1,2,0\n2,2,,0\n",encoding="utf-8")
    assert sparse_projection(path,{1},{1})=={(1,1):2.+0j}
    with pytest.raises(ValueError, match="PF coordinate"):
        sparse_projection(path,{2})


def test_participation_normalizes_magnitudes_not_complex_sum():
    shares=participation_shares({"a":3+4j,"b":-5j})
    assert shares=={"a":.5,"b":.5}
    with pytest.raises(ValueError):
        participation_shares({"a":0j})


def test_output_rejects_destinations_outside_the_comparator_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("POWER_MACHINE_OUTPUT_DIR", str(tmp_path / "workspace"))
    with pytest.raises(ValueError):
        fresh_output(tmp_path,"E39-MOD-02","test")
    with pytest.raises(ValueError):
        fresh_output(tmp_path,"E39-MOD-02","../other")


def test_comparator_patch_restored_even_on_failure():
    original=(rms_replay.build_rms_case,mod.build_rms_case,rms_replay.fresh_output,mod.fresh_output,mod.state_transform)
    with pytest.raises(RuntimeError):
        with comparator_options("baseline","test"):
            raise RuntimeError("synthetic failure")
    assert original==(rms_replay.build_rms_case,mod.build_rms_case,rms_replay.fresh_output,mod.fresh_output,mod.state_transform)


@pytest.fixture(scope="module")
def offline():
    if not DEFAULT_PF_DIR.exists():
        pytest.skip("Referencia offline ausente")
    baseline=build_variant(DEFAULT_PF_DIR,"baseline")
    variant=build_variant(DEFAULT_PF_DIR,"exponential_frozen")
    root=DEFAULT_PF_DIR/"E39-MOD-02"
    states=list(csv.DictReader((root/"states.csv").open(encoding="utf-8-sig")))
    return baseline,variant,states,root


def test_nine_avr_local_exponential_matches_pf_not_current_quadratic(offline):
    baseline,variant,states,root=offline
    ids={int(r["index"]) for r in states if r["state"]=="xe"}
    j=sparse_projection(root/"Jacobian.csv",ids,ids)
    deltas=[]
    mismatch=0
    for d,xs in zip(variant.devices,variant.solver.x_slices):
        if not getattr(d,"avr",None):
            continue
        x=d.parts(variant.initial.x[xs])[1]
        df,_=d.avr.jacobian(0.,x,d.avr_inputs)
        i=next(int(r["index"]) for r in states if r["model"]==pf_short(d.avr.relative_path) and r["state"]=="xe")
        deltas.append(abs(df[2,2]-j[i,i].real))
        bd=next(b for b in baseline.devices if b.object_id==d.object_id)
        bdf,_=bd.avr.jacobian(0.,x,bd.avr_inputs)
        mismatch+=abs(bdf[2,2]-j[i,i].real)>1e-3
    assert len(deltas)==9 and max(deltas)<1e-6
    assert mismatch==9


def test_full_variant_equilibrium_only_avr_changed_and_transform_bijective(offline):
    base,var,states,_=offline
    assert var.equilibrium["max_derivative_per_s"]<1e-8
    assert var.equilibrium["max_kcl_pu"]<1e-8
    assert var.solver.nx==166
    for bd,vd in zip(base.devices,var.devices):
        if getattr(bd,"avr",None):
            assert type(bd.avr) is IEEET1AVR
            assert type(vd.avr) is ExponentialIEEET1AVR
            assert dict(bd.avr.parameters)==dict(vd.avr.parameters)
            assert type(bd.governor)==type(vd.governor)
            assert dict(bd.governor.parameters)==dict(vd.governor.parameters)
    t,mapping=coordinate_transform(var,states)
    assert np.linalg.matrix_rank(t)==166
    assert len(mapping)==166
    a,_=var.linearize()
    ma=mapped_matrix(a,t)
    xr=[i for i,r in enumerate(states) if "\\AVR " in r["model"] and r["state"]=="xr"]
    np.testing.assert_allclose(ma[xr],0.,atol=1e-9)

