# copyright ############################### #
# This file is part of the Xtrack Package.  #
# Copyright (c) CERN, 2021.                 #
# ######################################### #

import pathlib
import numpy as np
from scipy.constants import e as qe
from scipy.constants import c as clight
from scipy.constants import epsilon_0, hbar

import xpart as xp
import xtrack as xt
import xobjects as xo
from xobjects.test_helpers import for_all_test_contexts
from xpart.test_helpers import flaky_assertions, retry

test_data_folder = pathlib.Path(
        __file__).parent.joinpath('../test_data').absolute()


@for_all_test_contexts
def test_radiation(test_context):

    print(f"Test {test_context.__class__}")

    ctx2np = test_context.nparray_from_context_array

    L_bend = 1.
    B_T = 2

    delta = 0
    particles_ave = xp.Particles(
            _context=test_context,
            p0c=5e9 / (1 + delta), # 5 GeV
            x=np.zeros(1000000),
            px=1e-4,
            py=-1e-4,
            delta=delta,
            mass0=xp.ELECTRON_MASS_EV)
    particles_ave_0 = particles_ave.copy()
    gamma = ctx2np((particles_ave.energy/particles_ave.mass0))[0]
    gamma0 = ctx2np(particles_ave.gamma0)[0]
    particles_rnd = particles_ave.copy()

    P0_J = ctx2np(particles_ave.p0c[0]) / clight * qe
    h_bend = B_T * qe / P0_J
    theta_bend = h_bend * L_bend

    dipole_ave = xt.Multipole(knl=[theta_bend], length=L_bend, hxl=theta_bend,
                            radiation_flag=1, _context=test_context)
    dipole_rnd = xt.Multipole(knl=[theta_bend], length=L_bend, hxl=theta_bend,
                            radiation_flag=2, _context=test_context)


    dct_ave_before = particles_ave.to_dict()
    dct_rng_before = particles_rnd.to_dict()

    particles_ave._init_random_number_generator()
    particles_rnd._init_random_number_generator()

    dipole_ave.track(particles_ave)
    dipole_rnd.track(particles_rnd)

    dct_ave = particles_ave.to_dict()
    dct_rng = particles_rnd.to_dict()

    assert np.allclose(dct_ave['delta'], np.mean(dct_rng['delta']),
                    atol=0, rtol=5e-3)

    rho_0 = L_bend/theta_bend
    mass0_kg = (dct_ave['mass0']*qe/clight**2)
    r0 = qe**2/(4*np.pi*epsilon_0*mass0_kg*clight**2)
    Ps = (2 * r0 * clight * mass0_kg * clight**2 * gamma0**2 * gamma**2)/(3*rho_0**2) # W

    Delta_E_eV = -Ps*(L_bend/clight) / qe
    Delta_E_trk = (dct_ave['ptau']-dct_ave_before['ptau'])*dct_ave['p0c']

    assert np.allclose(Delta_E_eV, Delta_E_trk, atol=0, rtol=4e-5)

    # Check photons
    line=xt.Line(elements=[
                xt.Drift(length=1.0),
                xt.Multipole(knl=[theta_bend], length=L_bend, hxl=theta_bend),
                xt.Drift(length=1.0),
                xt.Multipole(knl=[theta_bend], length=L_bend, hxl=theta_bend)
                ])
    line.build_tracker(_context=test_context)
    line.configure_radiation(model='quantum')

    record_capacity = int(100e6)
    record = line.start_internal_logging_for_elements_of_type(xt.Multipole,
                                                                capacity=record_capacity)
    particles_test = particles_ave_0.copy()
    particles_test_before = particles_test.copy()
    line.track(particles_test)

    particles_test.move(xo.context_default)
    particles_test_before.move(xo.context_default)
    particles_ave.move(xo.context_default)
    record.move(xo.context_default)

    Delta_E_test = (particles_test.ptau - particles_test_before.ptau
                                                        )*particles_test.p0c
    n_recorded = record._index.num_recorded
    assert n_recorded < record_capacity
    assert np.allclose(-np.sum(Delta_E_test),
                    np.sum(record.photon_energy[:n_recorded]),
                    atol=0, rtol=1e-6)

    p0_J = particles_ave.p0c[0] / clight * qe
    B_T = p0_J / qe / rho_0
    mass_0_kg = particles_ave.mass0 * qe / clight**2
    E_crit_J = 3 * qe * hbar * gamma**2 * B_T / (2 * mass_0_kg)

    E_ave_J = 8 * np.sqrt(3) / 45 * E_crit_J
    E_ave_eV = E_ave_J / qe

    E_sq_ave_J = 11 / 27 * E_crit_J**2
    E_sq_ave_eV = E_sq_ave_J / qe**2

    assert np.isclose(np.mean(record.photon_energy[:n_recorded]),
                    E_ave_eV, rtol=1e-2, atol=0)
    assert np.isclose(np.std(record.photon_energy[:n_recorded]),
                    np.sqrt(E_sq_ave_eV - E_ave_eV**2), rtol=1e-3, atol=0)

@for_all_test_contexts
@retry()
def test_ring_with_radiation(test_context):

    from cpymad.madx import Madx

    # Import thick sequence
    mad = Madx()

    # CLIC-DR
    mad.call(str(test_data_folder.joinpath('clic_dr/sequence.madx')))
    mad.use('ring')

    # Twiss
    twthick = mad.twiss().dframe()

    # Emit
    mad.sequence.ring.beam.radiate = True
    mad.emit()
    mad_emit_table = mad.table.emit.dframe()
    mad_emit_summ = mad.table.emitsumm.dframe()

    # Makethin
    mad.input(f'''
    select, flag=MAKETHIN, SLICE=4, thick=false;
    select, flag=MAKETHIN, pattern=wig, slice=1;
    MAKETHIN, SEQUENCE=ring, MAKEDIPEDGE=true;
    use, sequence=RING;
    ''')
    mad.use('ring')
    mad.twiss()

    # Build xtrack line
    line = xt.Line.from_madx_sequence(mad.sequence['RING'])
    line.particle_ref = xp.Particles(
            mass0=xp.ELECTRON_MASS_EV,
            q0=-1,
            gamma0=mad.sequence.ring.beam.gamma)

    # Build tracker
    line.matrix_stability_tol = 1e-2
    line.build_tracker()

    line.configure_radiation(model='mean')

    # Twiss
    tw = line.twiss(eneloss_and_damping=True)

    # Checks
    met = mad_emit_table

    with flaky_assertions():
        assert np.isclose(tw['eneloss_turn'], mad_emit_summ.u0[0]*1e9,
                        rtol=3e-3, atol=0)
        assert np.isclose(tw['damping_constants_s'][0],
            met[met.loc[:, 'parameter']=='damping_constant']['mode1'][0],
            rtol=3e-3, atol=0
            )
        assert np.isclose(tw['damping_constants_s'][1],
            met[met.loc[:, 'parameter']=='damping_constant']['mode2'][0],
            rtol=1e-3, atol=0
            )
        assert np.isclose(tw['damping_constants_s'][2],
            met[met.loc[:, 'parameter']=='damping_constant']['mode3'][0],
            rtol=3e-3, atol=0
            )

        assert np.isclose(tw['partition_numbers'][0],
            met[met.loc[:, 'parameter']=='damping_partion']['mode1'][0],
            rtol=3e-3, atol=0
            )
        assert np.isclose(tw['partition_numbers'][1],
            met[met.loc[:, 'parameter']=='damping_partion']['mode2'][0],
            rtol=1e-3, atol=0
            )
        assert np.isclose(tw['partition_numbers'][2],
            met[met.loc[:, 'parameter']=='damping_partion']['mode3'][0],
            rtol=3e-3, atol=0
            )

    line.configure_radiation(model='mean')
    part_co = line.find_closed_orbit()

    par_for_emit = line.build_particles(
                                x_norm=50*[0],
                                zeta=part_co.zeta[0], delta=part_co.delta[0],
                                _context=test_context
                                )
    line.discard_tracker()
    line.build_tracker(test_context)
    line.configure_radiation(model='quantum')
    num_turns=1500
    line.track(par_for_emit, num_turns=num_turns, turn_by_turn_monitor=True)
    mon = line.record_last_track

    with flaky_assertions():
        assert np.isclose(np.std(mon.zeta[:, 750:]),
            np.sqrt(met[met.loc[:, 'parameter']=='emittance']['mode3'][0] * np.abs(tw['betz0'])),
            rtol=0.2, atol=0
            )

        assert np.isclose(np.std(mon.x[:, 750:]),
            np.sqrt(met[met.loc[:, 'parameter']=='emittance']['mode1'][0] * tw['betx'][0]),
            rtol=0.2, atol=0
            )

        assert np.all(mon.y[:] < 1e-15)

    # Test particles generation (with electrons)
    bunch_intensity = 1e11
    sigma_z = 5e-3
    n_part = int(5e5)
    nemitt_x = 0.5e-6
    nemitt_y = 0.5e-6

    line.discard_tracker()
    line.build_tracker(_context=xo.context_default)
    line.configure_radiation(model='mean')
    pgen = xp.generate_matched_gaussian_bunch(line=line,
            num_particles=n_part, total_intensity_particles=bunch_intensity,
            nemitt_x=nemitt_x, nemitt_y=nemitt_y, sigma_z=sigma_z,
            _context=test_context)

    assert pgen._buffer.context is test_context
    pgen.move(_context=xo.ContextCpu())

    with flaky_assertions():
        assert np.isclose(np.std(pgen.x),
                        np.sqrt(tw['dx'][0]**2*np.std(pgen.delta)**2
                                + tw['betx'][0]*nemitt_x/mad.sequence.ring.beam.gamma),
                        atol=0, rtol=1e-2)

        assert np.isclose(np.std(pgen.y),
                        np.sqrt(tw['dy'][0]**2*np.std(pgen.delta)**2
                                + tw['bety'][0]*nemitt_y/mad.sequence.ring.beam.gamma),
                        atol=0, rtol=1e-2)

        assert np.isclose(np.std(pgen.zeta), sigma_z, atol=0, rtol=5e-3)
