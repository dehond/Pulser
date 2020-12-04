import itertools

import qutip
import numpy as np
import matplotlib.pyplot as plt

from pulser import Pulse

print('simulation module...')


class Simulation:
    """Simulation of a pulse sequence using QuTiP."""

    def __init__(self, sequence):
        self._seq = sequence
        self._reg = sequence._device._register
        self._size = len(self._reg.qubits)
        self._tot_duration = max(
                        [self._seq._last(ch).tf for ch in self._seq._schedule]
                                )
        self._times = np.arange(self._tot_duration, dtype=np.double)
        self._qid_index = {qid: i for i, qid in enumerate(self._reg.qubits)}

        self.dim = 3  # Default value
        self.basis = {}
        self.op_matrix = {}

        self.samples = {addr: {basis: {}
                               for basis in ['ground-rydberg', 'digital']}
                        for addr in ['Global', 'Local']}
        self.addressing = {addr: {basis: False
                                  for basis in ['ground-rydberg', 'digital']}
                           for addr in ['Global', 'Local']}
        self.operators = {addr: {basis: {}
                                 for basis in ['ground-rydberg', 'digital']}
                          for addr in ['Global', 'Local']}

        self._extract_samples()
        self._decide_basis()
        self._create_basis_and_operators()
        self._construct_hamiltonian()

    def _extract_samples(self):

        def prepare_dict():
            # Duration includes retargeting, delays, etc.
            return {'amp': np.zeros(self._tot_duration),
                    'det': np.zeros(self._tot_duration),
                    'phase': np.zeros(self._tot_duration)}

        def write_samples(slot, samples_dict):
            samples_dict['amp'][slot.ti:slot.tf] += slot.type.amplitude.samples
            samples_dict['det'][slot.ti:slot.tf] += slot.type.detuning.samples
            samples_dict['phase'][slot.ti:slot.tf] = slot.type.phase

        for channel in self._seq.declared_channels:
            addr = self._seq.declared_channels[channel].addressing
            basis = self._seq.declared_channels[channel].basis
            self.addressing[addr][basis] = True

            samples_dict = self.samples[addr][basis]

            if addr == 'Global':
                if not samples_dict:
                    samples_dict = prepare_dict()
                for slot in self._seq._schedule[channel]:
                    if isinstance(slot.type, Pulse):
                        write_samples(slot, samples_dict)

            elif addr == 'Local':
                for slot in self._seq._schedule[channel]:
                    if isinstance(slot.type, Pulse):
                        for qubit in slot.targets:  # Allow multiaddressing
                            if qubit not in samples_dict:
                                samples_dict[qubit] = prepare_dict()
                            write_samples(slot, samples_dict[qubit])

            self.samples[addr][basis] = samples_dict

    def _decide_basis(self):
        """Decide appropriate basis."""
        if not self.addressing['Global']['digital']\
                and not self.addressing['Local']['digital']:
            self.basis_name = 'ground-rydberg'
        elif not self.addressing['Global']['ground-rydberg']\
                and not self.addressing['Local']['ground-rydberg']:
            self.basis_name = 'digital'
        else:
            self.basis_name = 'all'  # All three states

    def _create_basis_and_operators(self):
        """Create the basis elements."""
        if self.basis_name == 'all':
            self.dim = 3
            self.basis = {'r': qutip.basis(3, 0),
                          'g': qutip.basis(3, 1),
                          'h': qutip.basis(3, 2)
                          }
            self.op_matrix = {
                    'I': qutip.qeye(3),
                    'sigma_gr': self.basis['g'] * self.basis['r'].dag(),
                    'sigma_hg': self.basis['h'] * self.basis['g'].dag(),
                    'sigma_rr': self.basis['r'] * self.basis['r'].dag(),
                    'sigma_gg': self.basis['g'] * self.basis['g'].dag(),
                    'sigma_hh': self.basis['h'] * self.basis['h'].dag()
                               }
        elif self.basis_name == 'ground-rydberg':
            self.dim = 2
            self.basis = {'r': qutip.basis(2, 0),
                          'g': qutip.basis(2, 1)
                          }
            self.op_matrix = {
                    'I': qutip.qeye(2),
                    'sigma_gr': self.basis['g'] * self.basis['r'].dag(),
                    'sigma_rr': self.basis['r'] * self.basis['r'].dag(),
                    'sigma_gg': self.basis['g'] * self.basis['g'].dag()
                               }
        elif self.basis_name == 'digital':
            self.dim = 2
            self.basis = {'g': qutip.basis(2, 0),
                          'h': qutip.basis(2, 1)
                          }
            self.op_matrix = {
                    'I': qutip.qeye(2),
                    'sigma_hg': self.basis['h'] * self.basis['g'].dag(),
                    'sigma_hh': self.basis['h'] * self.basis['h'].dag(),
                    'sigma_gg': self.basis['g'] * self.basis['g'].dag()
                              }

    def _build_operator(self, op_id, *qubit_ids, global_op=False):
        if global_op:
            return sum([self._build_operator(op_id, q_id)
                        for q_id in self._reg.qubits])

        if len(set(qubit_ids)) < len(qubit_ids):
            raise ValueError("Duplicate atom ids in argument list.")
        # List of identity matrices with shape of operator:
        temp = [qutip.qeye(self.op_matrix[op_id].shape[0])
                for _ in range(self._size)]
        for q_id in qubit_ids:
            temp[self._qid_index[q_id]] = self.op_matrix[op_id]
        return qutip.tensor(temp)

    def _construct_hamiltonian(self):
        def make_vdw_term():
            """
            Construct the Van der Waals interaction Term.

            For each pair of qubits, calculate each distance pairwise, then
            assign the local operator "sigma_rr" on each pair.
            """
            vdw = 0
            # Get every pair without duplicates
            for qubit1, qubit2 in itertools.combinations(self._reg._ids, r=2):
                dist = np.linalg.norm(
                        self._reg.qubits[qubit1] - self._reg.qubits[qubit2])
                vdw += (1e6 / (dist**6)) * 0.5 * \
                    self._build_operator('sigma_rr', qubit1, qubit2)
            return [vdw]

        def build_coeffs_ops(basis, addr):
            samples = self.samples[addr][basis]
            operators = self.operators[addr][basis]
            # Choose operator names according to addressing:
            if basis == 'ground-rydberg':
                op_ids = ['sigma_gr', 'sigma_rr']
            elif basis == 'digital':
                op_ids = ['sigma_hg', 'sigma_gg']

            terms = []
            if addr == 'Global':
                coeffs = [samples['amp'] * np.exp(-1j * samples['phase']),
                          0.5 * samples['det']]
                for coeff, op_id in zip(coeffs, op_ids):
                    if np.any(coeff != 0):
                        # Build once global operators as they are needed
                        if op_id not in operators:
                            operators[op_id] =\
                                    self._build_operator(op_id, global_op=True)
                        terms.append([operators[op_id], coeff])
            elif addr == 'Local':
                for q_id, samples_q in samples.items():
                    if q_id not in operators:
                        operators[q_id] = {}
                    coeffs = [samples_q['amp'] * np.exp(-1j
                                                        * samples_q['phase']),
                              0.5 * samples_q['det']]
                    for coeff, op_id in zip(coeffs, op_ids):
                        if np.any(coeff != 0):
                            if op_id not in operators[q_id]:
                                operators[q_id][op_id] = \
                                    self._build_operator(op_id, q_id)
                            terms.append([operators[q_id][op_id], coeff])

            self.operators[addr][basis] = operators
            return terms

        # Time independent term:
        if self.basis_name == 'digital':
            qobj_list = []
        else:
            # Van der Waals Interaction Terms
            qobj_list = make_vdw_term()

        # Time dependent terms:
        for addr in self.addressing:
            for basis in self.addressing[addr]:
                if self.samples[addr][basis]:
                    qobj_list += build_coeffs_ops(basis, addr)

        ham = qutip.QobjEvo(qobj_list, tlist=self._times)
        ham = ham + ham.dag()
        ham.compress()
        self._hamiltonian = ham

    # Run Simulation Evolution using Qutip
    def run(self, initial_state=None, observable=None, plot=True,
            all_states=False, custom_label=None):
        """
        Simulate the sequence.

        Can either give a predefined observable, or the final state after
        evolution, or the list of all states during evolution. If plot,
        optionally add a custom label
        """
        if initial_state:
            psi0 = initial_state
        else:
            # by default, "all down" state
            all_down = [qutip.basis(self.dim, self.dim - 1)
                        for _ in range(self._size)]
            psi0 = qutip.tensor(all_down)

        if observable:
            # With observables, we get their expectation value
            result = qutip.sesolve(self._hamiltonian, psi0,
                                   self._times, [observable])
            self.output = result.expect[0]
            if plot:
                plt.plot(self._times, self.output, label=custom_label)
                plt.legend()
        else:
            # Without observables, we get the output state
            result = qutip.sesolve(self._hamiltonian, psi0,
                                   self._times)
            if all_states:
                self.output = result.states  # All states of evolution
            else:
                self.output = result.states[-1]  # Final state of evolution
