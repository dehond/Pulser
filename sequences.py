import warnings
import copy
import numpy as np
import matplotlib.pyplot as plt
from collections import namedtuple

from devices import PasqalDevice
from pulses import Pulse, ConstantWaveform
from utils import validate_duration

# Auxiliary class to store the information in the schedule
TimeSlot = namedtuple('TimeSlot', ['type', 'ti', 'tf', 'targets'])

# Auxiliary class to store a channel's phase reference
Phase = namedtuple('Phase', ['value'])


class Sequence:
    """A sequence of operations on a device.

    A sequence is composed by
        - The device in which we want to implement it
        - The device's channels that are used
        - The schedule of operations on each channel
    """
    def __init__(self, device):
        if not isinstance(device, PasqalDevice):
            raise TypeError("The Sequence's device has to be a PasqalDevice.")
        self._device = device
        self._channels = {}
        self._schedule = {}
        self._phase_ref = {}  # The phase reference of each channel
        self._taken_channels = []   # Stores the ids of selected channels
        self._qids = set(self.qubit_info.keys())  # IDs of all qubits in device

    @property
    def qubit_info(self):
        """Returns the dictionary with the qubit's IDs and positions."""
        return self._device.qubits

    @property
    def declared_channels(self):
        return dict(self._channels)

    @property
    def available_channels(self):
        return {id: ch for id, ch in self._device.channels.items()
                if id not in self._taken_channels}

    def declare_channel(self, name, channel_id, initial_target=None):
        """Declare a new channel to the Sequence.

        Args:
            name (str): Unique name for the channel in the sequence.
            channel_id (str): How the channel is identified in the device.

        Keyword Args:
            initial_target (set, default=None): For 'local' adressing channels
                where a target has to be defined, it can be done when the
                channel is first declared. If left as None, this will have to
                be done manually as the first addition to a channel.
        """

        if name in self._channels:
            raise ValueError("The given name is already in use.")

        if channel_id not in self._device.channels:
            raise ValueError("No channel %s in the device." % channel_id)

        if channel_id in self._taken_channels:
            raise ValueError("Channel %s has already been added." % channel_id)

        ch = self._device.channels[channel_id]
        self._channels[name] = ch
        self._taken_channels.append(channel_id)
        self._schedule[name] = []
        self._phase_ref[name] = [Phase(value=0)]

        if ch.addressing == 'global':
            self._schedule[name].append(TimeSlot('target', -1, 0, self._qids))
        elif initial_target is not None:
            self.target(initial_target, name)

    def add(self, pulse, channel, protocol='min-delay'):
        """Add a pulse to a channel.

        Args:
            pulse (Pulse): The pulse object to add to the channel.
            channel (str): The channel's name provided when declared.

        Keyword Args:
            protocol (default='min-delay'): Stipulates how to deal with
                eventual conflicts with other channels, specifically in terms
                of having to channels act on the same target simultaneously.
                'min-delay': Before adding the pulse, introduces the smallest
                    possible delay that avoids all exisiting conflicts.
                'no-delay': Adds the pulse to the channel, regardless of
                    existing conflicts.
                'wait-for-all': Before adding the pulse, adds a delay that
                    idles the channel until the end of the other channels'
                    latest pulse.
        """

        last = self._last(channel)
        self._validate_pulse(pulse, channel)

        valid_protocols = ['min-delay', 'no-delay', 'wait-for-all']
        if protocol not in valid_protocols:
            raise ValueError(f"Invalid protocol '{protocol}', only accepts "
                             "protocols: " + ", ".join(valid_protocols))

        t0 = last.tf    # Preliminary ti
        current_max_t = t0  # Stores the maximum tf found so far
        if protocol != 'no-delay':
            for ch, seq in self._schedule.items():
                if ch == channel:
                    continue
                for op in self._schedule[ch][::-1]:
                    if op.tf <= current_max_t:
                        break
                    if not isinstance(op.type, Pulse):
                        continue
                    if op.targets & last.targets or protocol == 'wait-for-all':
                        current_max_t = op.tf
                        break
        ti = current_max_t
        tf = ti + pulse.duration
        if ti > t0:
            self.delay(ti-t0, channel)

        phase_ref = self._phase_ref[channel][-1].value
        if phase_ref != 0:
            # Has to copy to keep the original pulse intact
            pulse = copy.deepcopy(pulse)
            pulse.phase = (pulse.phase + phase_ref) % (2 * np.pi)

        self._add_to_schedule(channel, TimeSlot(pulse, ti, tf, last.targets))

        if pulse.post_phase_shift:
            self.phase_shift(pulse.post_phase_shift, channel)

    def target(self, qubits, channel):
        """Changes the target qubit of a 'local' channel.

        Args:
            qubits (set(str)): The new target for this channel.
            channel (str): The channel's name provided when declared.
        """

        if channel not in self._channels:
            raise ValueError("Use the name of a declared channel.")

        qs = {qubits}
        if not qs.issubset(self._qids):
            raise ValueError("The given qubits have to belong to the device.")

        if self._channels[channel].addressing != 'local':
            raise ValueError("Can only choose target of 'local' channels.")
        elif len(qs) != 1:
            raise ValueError("This channel takes only a single target qubit.")

        try:
            last = self._last(channel)
            if last.targets == qs:
                warnings.warn("The provided qubits are already the target. "
                              "Skipping this target instruction.")
                return
            ti = last.tf
            tf = ti + self._channels[channel].retarget_time
        except ValueError:
            ti = -1
            tf = 0

        self._add_to_schedule(channel, TimeSlot('target', ti, tf, qs))

    def delay(self, duration, channel):
        """Idle a given choosen for a specific duration.

        Args:
            duration (int): Time to delay (in ns).
            channel (str): The channel's name provided when declared.
        """
        last = self._last(channel)
        ti = last.tf
        tf = ti + validate_duration(duration)
        self._add_to_schedule(channel, TimeSlot('delay', ti, tf, last.targets))

    def measure(self, basis='ground-rydberg'):
        """Measure in a valid basis.

        Args:
            basis (str): Valid basis for measurement (consult the
                'supported_basis_states' attribute of the selected device for
                the available options).
        """
        available = self._device.supported_basis_states
        if basis not in available:
            raise ValueError(f"The basis '{basis}' is not support by the "
                             "selected device. The available options are: "
                             + ", ".join(list(available)))

        if hasattr(self, '_measurement'):
            raise ValueError("The sequence has already been measured.")

        self._measurement = basis

    def phase_shift(self, phi, channel):
        """Shift the phase of a channel's reference by 'phi'.

        This is equivalent to an Rz(phi) gate (i.e. a rotation of the target
        qubit's state by an angle phi around the z-axis of the Bloch sphere).

        Args:
            phi (float): The intended phase shift (in rads).
        """
        if phi == 0:
            warnings.warn("A phase shift of 0 is meaningless, "
                          "it will be ommited.")
            return
        last = self._last(channel)
        ti = last.tf
        self._add_to_schedule(
            channel, TimeSlot(Phase(phi), ti, ti, last.targets))
        new_phase = (self._phase_ref[channel][-1].value + phi) % (2 * np.pi)
        self._phase_ref[channel].append(Phase(new_phase))

    def draw(self):
        """Draw the entire sequence."""
        n_channels = len(self._channels)
        if not n_channels:
            raise SystemError("Can't draw an empty sequence.")
        data = self._gather_data()
        time_scale = 1e3 if self._total_duration > 1e4 else 1

        fig = plt.figure(constrained_layout=False, figsize=(20, 4.5*n_channels))
        gs = fig.add_gridspec(n_channels, 1, hspace=0.075)

        ch_axes = {}
        for i, (ch, gs_) in enumerate(zip(self._channels, gs)):
            ax = fig.add_subplot(gs_)
            ax.spines['top'].set_color('none')
            ax.spines['bottom'].set_color('none')
            ax.spines['left'].set_color('none')
            ax.spines['right'].set_color('none')
            ax.tick_params(labelcolor='w', top=False, bottom=False, left=False,
                           right=False)
            ax.set_ylabel(ch, labelpad=40, fontsize=18)
            subgs = gs_.subgridspec(2, 1, hspace=0.)
            ax1 = fig.add_subplot(subgs[0, :])
            ax2 = fig.add_subplot(subgs[1, :])
            ch_axes[ch] = (ax1, ax2)
            for j, ax in enumerate(ch_axes[ch]):
                ax.axvline(0, linestyle='--', linewidth=0.5, color='grey')
                if j == 0:
                    ax.spines['bottom'].set_visible(False)
                else:
                    ax.spines['top'].set_visible(False)

                if i < n_channels - 1 or j == 0:
                    ax.tick_params(axis='x', which='both', bottom=True,
                                   top=False, labelbottom=False, direction='in')
                else:
                    unit = 'ns' if time_scale == 1 else r'$\mu s$'
                    ax.set_xlabel(f't ({unit})', fontsize=12)

        for ch, (a, b) in ch_axes.items():
            t = np.array(data[ch]['time']) / time_scale
            ya = data[ch]['amp']
            yb = data[ch]['detuning']

            t_min = -t[-1]*0.03
            t_max = t[-1]*1.05
            a.set_xlim(t_min, t_max)
            b.set_xlim(t_min, t_max)

            max_amp = np.max(ya)
            max_amp = 1 if max_amp == 0 else max_amp
            amp_top = max_amp * 1.2
            a.set_ylim(-0.02, amp_top)
            det_max = np.max(yb)
            det_min = np.min(yb)
            det_range = det_max - det_min
            if det_range == 0:
                det_min, det_max, det_range = -1, 1, 2
            det_top = det_max + det_range * 0.15
            det_bottom = det_min - det_range * 0.05
            b.set_ylim(det_bottom, det_top)

            a.plot(t, ya, color="darkgreen", linewidth=0.8)
            b.plot(t, yb, color='indigo', linewidth=0.8)
            a.fill_between(t, 0, ya, color="darkgreen", alpha=0.3)
            b.fill_between(t, 0, yb, color="indigo", alpha=0.3)
            a.set_ylabel('Amplitude (MHz)', fontsize=12, labelpad=10)
            b.set_ylabel('Detuning (MHz)', fontsize=12)

            for coords in data[ch]['target']:
                targets = list(data[ch]['target'][coords])
                tgt_txt_y = max_amp*1.1-0.25*(len(targets)-1)
                tgt_str = "\n".join([str(q) for q in targets])
                if coords == 'initial':
                    x = t_min + t[-1]*0.005
                    if self._channels[ch].addressing == 'global':
                        a.text(x, amp_top*0.98, "GLOBAL",
                               fontsize=13, rotation=90, ha='left', va='top',
                               bbox=dict(boxstyle="round", facecolor='orange'))
                    else:
                        a.text(x, tgt_txt_y, tgt_str, fontsize=12, ha='left',
                               bbox=dict(boxstyle="round", facecolor='orange'))
                else:
                    ti, tf = coords
                    a.axvspan(ti, tf, alpha=0.4, color='grey', hatch='//')
                    b.axvspan(ti, tf, alpha=0.4, color='grey', hatch='//')
                    a.text(tf + t[-1]*0.006, tgt_txt_y, tgt_str, fontsize=12,
                           bbox=dict(boxstyle="round", facecolor='orange'))

            for ti, phase in data[ch]['phase_shift'].items():
                a.axvline(ti, linestyle='--', linewidth=1.5, color='black')
                b.axvline(ti, linestyle='--', linewidth=1.5, color='black')
                value = (((phase + np.pi) % (2*np.pi)) - np.pi) / np.pi
                if value == -1:
                    msg = u"\u27F2 " + r"$\pi$"
                else:
                    msg = u"\u27F2 " + r"{:.2g}$\pi$".format(value)
                a.text(ti, max_amp*1.1, msg, ha='right', fontsize=14,
                       bbox=dict(boxstyle="round", facecolor='ghostwhite'))

            if 'measurement' in data[ch]:
                msg = f"Basis: {data[ch]['measurement']}"
                b.text(t[-1]*1.025, det_top, msg, ha='center', va='center',
                       fontsize=14, color='white', rotation=90)
                a.axvspan(t[-1], t_max, color='midnightblue', alpha=1)
                b.axvspan(t[-1], t_max, color='midnightblue', alpha=1)
                a.axhline(0, xmax=0.95, linestyle='-', linewidth=0.5,
                          color='grey')
                b.axhline(0, xmax=0.95, linestyle=':', linewidth=0.5,
                          color='grey')
            else:
                a.axhline(0, linestyle='-', linewidth=0.5, color='grey')
                b.axhline(0, linestyle=':', linewidth=0.5, color='grey')

        plt.show()

    def __str__(self):
        full = ""
        std_line = "t: {}->{} | {} | Targets: {}\n"
        delay_line = "t: {}->{} | Delay \n"
        phase_line = "t: {} | Phase shift of: {:.3f}\n"
        for ch, seq in self._schedule.items():
            full += f"Channel: {ch}\n"
            first_slot = True
            for ts in seq:
                if first_slot:
                    full += f"t: 0 | Initial targets: {ts.targets}\n"
                    first_slot = False
                elif isinstance(ts.type, Pulse) or ts.type == 'target':
                    full += std_line.format(ts.ti, ts.tf, ts.type, ts.targets)
                elif ts.type == 'delay':
                    full += delay_line.format(ts.ti, ts.tf)
                else:
                    full += phase_line.format(ts.ti, ts.type.value)
            full += "\n"

        if hasattr(self, "_measurement"):
            full += f"Measured in basis: {self._measurement}"

        return full

    def _add_to_schedule(self, channel, timeslot):
        if hasattr(self, "_measurement"):
            raise ValueError("The sequence has already been measured. "
                             "Nothing more can be added.")
        self._schedule[channel].append(timeslot)

    def _last(self, channel):
        """Shortcut to last element in the channel's schedule."""
        if channel not in self._schedule:
            raise ValueError("Use the name of a declared channel.")
        try:
            return self._schedule[channel][-1]
        except IndexError:
            raise ValueError("The chosen channel has no target.")

    def _validate_pulse(self, pulse, channel):
        if not isinstance(pulse, Pulse):
            raise TypeError("pulse input must be of type Pulse, not of type "
                            "{}.".format(type(pulse)))

        ch = self._channels[channel]
        if np.any(pulse.amplitude.samples > ch.max_amp):
            raise ValueError("The pulse's amplitude goes over the maximum "
                             "value allowed for the chosen channel.")
        if np.any(np.abs(pulse.detuning.samples) > ch.max_abs_detuning):
            raise ValueError("The pulse's detuning values go out of the range "
                             "allowed for the chosen channel.")

    def _gather_data(self):
        """Collects the whole sequence data for plotting."""
        # The minimum time axis length is 100 ns
        self._total_duration = max([self._last(ch).tf for ch in self._schedule
                                    if self._schedule[ch]] + [100])
        data = {}
        for ch, seq in self._schedule.items():
            time = [-1]     # To not break the "time[-1]" later on
            amp = []
            detuning = []
            target = {}
            phase_shift = {}
            for slot in seq:
                if slot.ti == -1:
                    target['initial'] = slot.targets
                    time += [0]
                    amp += [0]
                    detuning += [0]
                    continue
                if slot.type in ['delay', 'target']:
                    time += [slot.ti, slot.tf-1]
                    amp += [0, 0]
                    detuning += [0, 0]
                    if slot.type == 'target':
                        target[(slot.ti, slot.tf-1)] = slot.targets
                    continue
                elif isinstance(slot.type, Phase):
                    phase_shift[slot.ti] = slot.type.value
                    continue
                pulse = slot.type
                if (isinstance(pulse.amplitude, ConstantWaveform) and
                        isinstance(pulse.detuning, ConstantWaveform)):
                    time += [slot.ti, slot.tf-1]
                    amp += [pulse.amplitude._value] * 2
                    detuning += [pulse.detuning._value] * 2
                else:
                    time += list(range(slot.ti, slot.tf))
                    amp += pulse.amplitude.samples.tolist()
                    detuning += pulse.detuning.samples.tolist()
            if time[-1] < self._total_duration - 1:
                time += [time[-1]+1, self._total_duration-1]
                amp += [0, 0]
                detuning += [0, 0]
            # Store everything
            time.pop(0)     # Removes the -1 in the beginning
            data[ch] = {'time': time, 'amp': amp, 'detuning': detuning,
                        'target': target, 'phase_shift': phase_shift}
            if hasattr(self, "_measurement"):
                data[ch]['measurement'] = self._measurement
        return data
