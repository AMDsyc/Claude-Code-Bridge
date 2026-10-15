# Claude Code Bridge - a review loop for Claude Code sessions
# Copyright (C) 2026  AMDsyc
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Bridge — a local watchdog for Claude Code sessions."""
__version__ = "0.1.0"

import os as _os

# A child of a test run whose suite fenced the network (netfence.py): the
# suite named its record in BRIDGE_NETFENCE, and a daemon it started is fenced
# from its first import on. Nothing running the bridge for real sets it.
if _os.environ.get("BRIDGE_NETFENCE"):
    from . import netfence as _netfence
    _netfence.fence(_os.environ["BRIDGE_NETFENCE"])
