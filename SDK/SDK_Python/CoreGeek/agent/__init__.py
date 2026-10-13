"""Future War v1.0 competition agent. Python 3.11+, standard library only."""

# Install narrow scheduling fixes before brain imports robot_assault symbols.
from .robot_assault_yield_patch import install as _install_robot_assault_yield_patch

_install_robot_assault_yield_patch()
del _install_robot_assault_yield_patch
