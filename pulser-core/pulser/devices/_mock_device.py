# Copyright 2020 Pulser Development Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pulser.channels import Microwave, Raman, Rydberg
from pulser.devices._device_datacls import VirtualDevice

MockDevice = VirtualDevice(
    name="MockDevice",
    dimensions=3,
    rydberg_level=70,
    max_atom_num=None,
    max_radial_distance=None,
    min_atom_distance=0.0,
    supports_slm_mask=True,
    _channels=(
        ("rydberg_global", Rydberg.Global(None, None, max_duration=None)),
        ("rydberg_local", Rydberg.Local(None, None, max_duration=None)),
        ("raman_global", Raman.Global(None, None, max_duration=None)),
        ("raman_local", Raman.Local(None, None, max_duration=None)),
        ("mw_global", Microwave.Global(None, None, max_duration=None)),
    ),
)
